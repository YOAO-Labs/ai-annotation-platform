CREATE FUNCTION yoao_assets_v1.lock_original_item(p_item uuid, p_wait boolean)
RETURNS TABLE(id uuid, dataset_id uuid, file_path text, file_type text, width integer, height integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_wait IS TRUE THEN
        RETURN QUERY SELECT i.id, i.dataset_id, i.file_path::text, i.file_type::text, i.width, i.height
            FROM public.dataset_items i WHERE i.id = p_item FOR SHARE;
    ELSE
        RETURN QUERY SELECT i.id, i.dataset_id, i.file_path::text, i.file_type::text, i.width, i.height
            FROM public.dataset_items i WHERE i.id = p_item FOR SHARE NOWAIT;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_asset_for_item(p_item uuid, p_lock boolean)
RETURNS TABLE(id uuid, dataset_item_id uuid, media_type text, checksum_algorithm text,
              checksum_value text, size_bytes bigint, verification_state text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        PERFORM 1 FROM public.dataset_assets a WHERE a.dataset_item_id = p_item FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT a.id, a.dataset_item_id, a.media_type::text, a.checksum_algorithm::text,
            a.checksum_value::text, a.size_bytes, a.verification_state::text
        FROM public.dataset_assets a WHERE a.dataset_item_id = p_item;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.managed_original_locations(p_asset uuid, p_lock boolean, p_bounded boolean)
RETURNS TABLE(asset_id uuid, location_id uuid, bucket text, object_key text, role text,
              reference_state text, creation_id uuid, storage_ref jsonb, storage_ref_oversized boolean,
              verified_at timestamptz, etag text, etag_oversized boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_location public.dataset_asset_locations%ROWTYPE;
    v_locations public.dataset_asset_locations[];
BEGIN
    IF p_lock IS TRUE THEN
        SELECT array_agg(s.locator ORDER BY (s.locator).location_id,(s.locator).bucket) INTO v_locations
        FROM (SELECT l AS locator FROM public.dataset_asset_locations l
            WHERE l.asset_id = p_asset AND l.role = 'managed_original' AND l.reference_state = 'present'
            ORDER BY l.location_id,l.bucket LIMIT 2 FOR SHARE NOWAIT) s;
    ELSE
        SELECT array_agg(s.locator ORDER BY (s.locator).location_id,(s.locator).bucket) INTO v_locations
        FROM (SELECT l AS locator FROM public.dataset_asset_locations l
            WHERE l.asset_id = p_asset AND l.role = 'managed_original' AND l.reference_state = 'present'
            ORDER BY l.location_id,l.bucket LIMIT 2) s;
    END IF;
    IF v_locations IS NULL THEN RETURN; END IF;
    FOREACH v_location IN ARRAY v_locations LOOP
        asset_id := v_location.asset_id;
        location_id := v_location.location_id;
        bucket := v_location.bucket;
        object_key := v_location.object_key;
        role := v_location.role;
        reference_state := v_location.reference_state;
        creation_id := v_location.creation_id;
        storage_ref_oversized := p_bounded IS TRUE AND octet_length(v_location.storage_ref::text) > 8192;
        storage_ref := CASE WHEN storage_ref_oversized THEN NULL ELSE v_location.storage_ref END;
        verified_at := v_location.verified_at;
        etag_oversized := p_bounded IS TRUE AND octet_length(v_location.etag) > 1024;
        etag := CASE WHEN etag_oversized THEN NULL ELSE v_location.etag END;
        RETURN NEXT;
    END LOOP;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.task_original_source(p_original uuid, p_lock boolean)
RETURNS TABLE(id uuid, creation_id uuid, task_id uuid, project_id uuid, location_id uuid,
    storage_identity text, bucket text, object_key text, sha256 text, size_bytes bigint,
    etag text, version_id text, created_at timestamptz, storage_ref jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        PERFORM 1 FROM public.task_originals t WHERE t.id = p_original FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT t.id, t.creation_id, t.task_id, t.project_id, t.location_id,
        t.storage_identity::text, t.bucket::text, t.object_key::text, t.sha256::text, t.size_bytes,
        t.etag, t.version_id, t.created_at, t.storage_ref
        FROM public.task_originals t WHERE t.id = p_original;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.auxiliary_original_source(p_original uuid, p_lock boolean)
RETURNS TABLE(id uuid, creation_id uuid, dataset_id uuid, kind text, source_contract jsonb,
    location_id uuid, storage_identity text, bucket text, object_key text, sha256 text,
    size_bytes bigint, etag text, version_id text, created_at timestamptz, storage_ref jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        PERFORM 1 FROM public.dataset_auxiliary_originals a WHERE a.id = p_original FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT a.id, a.creation_id, a.dataset_id, a.kind::text,
        CASE WHEN octet_length(a.source_contract::text) <= 131072 THEN a.source_contract END,
        a.location_id, a.storage_identity::text, a.bucket::text, a.object_key::text, a.sha256::text, a.size_bytes,
        CASE WHEN octet_length(a.etag) <= 1024 THEN a.etag END, a.version_id, a.created_at, a.storage_ref
        FROM public.dataset_auxiliary_originals a WHERE a.id = p_original;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.auxiliary_original_item(p_item uuid, p_lock boolean)
RETURNS TABLE(id uuid, dataset_id uuid, file_path text, file_type text, width integer, height integer,
    scene_id uuid, frame_index integer, nuscenes_raw_original_id uuid, metadata jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        PERFORM 1 FROM public.dataset_items i WHERE i.id = p_item FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT i.id, i.dataset_id, i.file_path::text, i.file_type::text,
        i.width, i.height, i.scene_id, i.frame_index, i.nuscenes_raw_original_id,
        CASE WHEN octet_length(i.metadata::text) <= 131072
            THEN jsonb_build_object('nuscenes_export', i.metadata->'nuscenes_export') END
        FROM public.dataset_items i WHERE i.id = p_item;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.auxiliary_original_scene(p_scene uuid, p_lock boolean)
RETURNS TABLE(id uuid, dataset_id uuid, source_format text, nuscenes_map_original_id uuid, source_metadata jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        PERFORM 1 FROM public.scenes s WHERE s.id = p_scene FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT s.id, s.dataset_id, s.source_format::text, s.nuscenes_map_original_id,
        CASE WHEN octet_length(s.source_metadata::text) <= 131072 THEN
            jsonb_build_object('frame', s.source_metadata->'frame', 'nuscenes_export', s.source_metadata->'nuscenes_export') END
        FROM public.scenes s WHERE s.id = p_scene;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_task_original_source(p_creation uuid)
RETURNS TABLE(id uuid, creation_id uuid, task_id uuid, project_id uuid, location_id uuid,
    storage_identity text, bucket text, object_key text, sha256 text, size_bytes bigint,
    etag text, version_id text, created_at timestamptz, storage_ref jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.task_originals t WHERE t.id = p_creation) THEN
        INSERT INTO public.task_originals (id, creation_id, task_id, project_id, location_id,
            storage_identity, bucket, object_key, sha256, size_bytes, etag, version_id, created_at, storage_ref)
        SELECT c.id, c.id, c.task_id, c.project_id, c.location_id, c.storage_identity, c.bucket,
            c.object_key, c.sha256, c.size_bytes, c.etag, c.version_id, c.completed_at,
            CASE WHEN c.configuration->'schema' = '2'::jsonb THEN nullif(c.configuration->'original_ref', 'null'::jsonb) END
        FROM public.managed_original_creations c WHERE c.id = p_creation
            AND c.owner_kind = 'task' AND c.status = 'verified' AND c.origin = 'direct'
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN QUERY SELECT * FROM yoao_assets_v1.task_original_source(p_creation, false);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_auxiliary_original_source(p_creation uuid)
RETURNS TABLE(id uuid, creation_id uuid, dataset_id uuid, kind text, source_contract jsonb,
    location_id uuid, storage_identity text, bucket text, object_key text, sha256 text,
    size_bytes bigint, etag text, version_id text, created_at timestamptz, storage_ref jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO public.dataset_auxiliary_originals (id, creation_id, dataset_id, kind, source_contract,
        location_id, storage_identity, bucket, object_key, sha256, size_bytes, etag, version_id, created_at, storage_ref)
    SELECT c.id, c.id, c.dataset_id, c.authority->'auxiliary'->>'kind', c.authority->'auxiliary'->'source_contract',
        c.location_id, c.storage_identity, c.bucket, c.object_key, c.sha256, c.size_bytes,
        c.etag, c.version_id, c.completed_at,
        CASE WHEN c.configuration->'schema' = '2'::jsonb THEN nullif(c.configuration->'original_ref', 'null'::jsonb) END
    FROM public.managed_original_creations c WHERE c.id = p_creation
        AND c.owner_kind = 'dataset_auxiliary' AND c.status = 'verified'
    ON CONFLICT ON CONSTRAINT dataset_auxiliary_originals_pkey DO NOTHING;
    RETURN QUERY SELECT * FROM yoao_assets_v1.auxiliary_original_source(p_creation, false);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.lock_original_item(uuid, boolean),
    yoao_assets_v1.original_asset_for_item(uuid, boolean),
    yoao_assets_v1.managed_original_locations(uuid, boolean, boolean),
    yoao_assets_v1.task_original_source(uuid, boolean),
    yoao_assets_v1.auxiliary_original_source(uuid, boolean),
    yoao_assets_v1.auxiliary_original_item(uuid, boolean),
    yoao_assets_v1.auxiliary_original_scene(uuid, boolean),
    yoao_assets_v1.publish_task_original_source(uuid),
    yoao_assets_v1.publish_auxiliary_original_source(uuid)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.managed_original_location_ref(
    p_asset uuid, p_location uuid, p_bucket text, p_key text
)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT l.storage_ref FROM public.dataset_asset_locations l
    WHERE l.asset_id = p_asset AND l.location_id = p_location AND l.bucket = p_bucket
        AND l.object_key = p_key AND l.role = 'managed_original' AND l.reference_state = 'present'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_binding_history()
RETURNS TABLE(task_originals bigint, creation_allocations bigint, creation_staging bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT (SELECT count(*) FROM public.task_originals),
        count(*) FILTER (WHERE c.owner_kind = 'task'),
        count(*) FILTER (WHERE c.staging_key IS NOT NULL)
    FROM public.managed_original_creations c
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.managed_original_location_ref(uuid, uuid, text, text),
    yoao_assets_v1.original_binding_history()
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.local_original_receipt(p_creation uuid)
RETURNS SETOF public.managed_original_creations
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.* FROM public.managed_original_creations c WHERE c.id = p_creation
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.local_original_for_owner_key(p_kind text,p_owner uuid,p_key text)
RETURNS SETOF public.managed_original_creations
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.* FROM public.managed_original_creations c
    WHERE c.owner_kind = p_kind AND c.object_key = p_key
        AND ((p_kind = 'dataset' AND c.dataset_id = p_owner) OR (p_kind = 'task' AND c.task_id = p_owner))
    ORDER BY (c.status = 'verified') DESC, c.allocated_at DESC, c.id DESC LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.local_original_for_owner_key(text,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.abandon_unstarted_fixture_original(
    p_run uuid,p_admission uuid,p_creation uuid,p_auxiliary boolean
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (
        UPDATE public.managed_original_creations c SET status = 'failed', completed_at = now(),
            error_code = CASE WHEN p_auxiliary THEN 'fixture_auxiliary_not_started' ELSE 'fixture_item_not_prepared' END
        WHERE c.id = p_creation AND c.status = 'allocated' AND c.operation_id IS NULL
            AND c.prepared IS NULL AND c.write_started_at IS NULL AND c.receipt IS NULL
            AND ((p_auxiliary AND c.owner_kind = 'dataset_auxiliary' AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_auxiliary_intents a
                WHERE a.creation_id = p_creation AND a.run_id = p_run AND a.admission_id = p_admission
            )) OR (NOT p_auxiliary AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources f
                WHERE f.run_id = p_run AND f.admission_id = p_admission AND f.creation_id = p_creation
                    AND f.kind = 'creation'
            )))
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._managed_original_locator_facts(c public.managed_original_creations)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT jsonb_build_object(
        'creation_id', c.id, 'dataset_id', c.dataset_id, 'location_id', c.location_id,
        'connection_id', c.configuration->'location'->'connection_id',
        'connection_revision', c.configuration->'connection_revision',
        'bucket', c.bucket, 'object_key', c.object_key, 'file_path', c.object_key,
        'key_prefix', coalesce(c.configuration->'original_ref'->>'key_prefix',''),
        'endpoint_url', c.configuration->'original_ref'->>'endpoint_url',
        'storage_identity', c.storage_identity,
        'config_revision', (c.configuration->'original_ref'->>'config_revision')::integer,
        'binding_generation', (c.configuration->'original_ref'->>'binding_generation')::integer,
        'max_bytes', c.max_bytes, 'content_type', c.content_type, 'origin', c.origin,
        'mode', coalesce(c.configuration->>'mode','write'),
        'staging', CASE WHEN c.staging_bucket IS NOT NULL THEN jsonb_build_object(
            'bucket',c.staging_bucket,'key',c.staging_key) END,
        'publication_id', nullif(c.authority->>'publication_id','')::uuid)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.fixture_auxiliary_original_material(p_creation uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_assets_v1._managed_original_locator_facts(c)
    FROM public.managed_original_creations c
    JOIN yoao_fixture_v1.fixture_auxiliary_intents a ON a.creation_id = c.id
    WHERE c.id = p_creation AND c.owner_kind = 'dataset_auxiliary'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.abandon_unstarted_fixture_original(uuid,uuid,uuid,boolean),
    yoao_assets_v1._managed_original_locator_facts(public.managed_original_creations),
    yoao_assets_v1.fixture_auxiliary_original_material(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.fixture_original_target_matches(p_creation uuid,p_require_owner boolean)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.managed_original_creations c
        JOIN yoao_fixture_v1.fixture_original_targets t
          ON t.id::text = c.configuration->'fixture_original_target'->>'id'
        WHERE c.id = p_creation AND t.location_id = c.location_id AND t.bucket = c.bucket
          AND t.storage_identity = c.storage_identity
          AND to_jsonb(t.generation) = c.configuration->'fixture_original_target'->'generation'
          AND to_jsonb(t.generation) = c.configuration->'original_ref'->'binding_generation'
          AND t.store_receipt_sha256 = c.configuration->'fixture_original_target'->>'store_receipt_sha256'
          AND (NOT p_require_owner OR EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_run_resources f
              JOIN yoao_fixture_v1.fixture_runs child ON child.id = f.run_id
              JOIN yoao_fixture_v1.fixture_runs root ON root.id = coalesce(child.parent_run_id,child.id)
              WHERE f.resource_id = c.id AND f.kind IN ('creation','auxiliary')
                AND root.original_target_id = t.id AND root.original_storage_identity = t.storage_identity
                AND root.original_store_receipt_sha256 = t.store_receipt_sha256
          ))
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.fixture_original_target_matches(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.allocate_local_original(p_request jsonb)
RETURNS SETOF public.managed_original_creations
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.managed_original_creations (
        id, owner_kind, dataset_id, project_id, task_id, location_id, storage_identity,
        bucket, object_key, logical_path, origin, max_bytes, timeout_seconds, content_type,
        requested_by, authority, configuration, input_fingerprint, maintenance_revision,
        staging_ref, staging_bucket, staging_key, status
    )
    SELECT x.id, x.owner_kind, x.dataset_id, x.project_id, x.task_id, x.location_id,
        x.storage_identity, x.bucket, x.object_key, x.logical_path, x.origin, x.max_bytes,
        x.timeout_seconds, x.content_type, x.requested_by, x.authority, x.configuration,
        x.input_fingerprint, x.maintenance_revision, x.staging_ref, x.staging_bucket,
        x.staging_key, 'allocated'
    FROM jsonb_to_record(p_request) AS x(
        id uuid, owner_kind text, dataset_id uuid, project_id uuid, task_id uuid,
        location_id uuid, storage_identity text, bucket text, object_key text, logical_path text,
        origin text, max_bytes bigint, timeout_seconds integer, content_type text, requested_by uuid,
        authority jsonb, configuration jsonb, input_fingerprint text, maintenance_revision integer,
        staging_ref jsonb, staging_bucket text, staging_key text
    )
    RETURNING *
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_local_original_step(
    p_creation uuid, p_step text, p_expected text, p_facts jsonb
)
RETURNS SETOF public.managed_original_creations
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_record public.managed_original_creations;
BEGIN
    CASE p_step
        WHEN 'claim' THEN
            UPDATE public.managed_original_creations
            SET status = 'preparing', claimed_at = (p_facts->>'claimed_at')::timestamptz
            WHERE id = p_creation AND status = 'allocated' RETURNING * INTO v_record;
        WHEN 'bind_permit' THEN
            UPDATE public.managed_original_creations c
            SET operation_id = (p_facts->>'operation_id')::uuid
            WHERE c.id = p_creation AND c.status = 'preparing' AND c.operation_id IS NULL
                AND EXISTS (
                    SELECT 1 FROM public.storage_operations o WHERE o.id = (p_facts->>'operation_id')::uuid
                        AND o.location_id = c.location_id AND o.revision = c.maintenance_revision
                        AND o.purpose = CASE WHEN c.owner_kind = 'task'
                            THEN 'task_original_write_' || replace(c.id::text,'-','')
                            ELSE 'managed_original_creation' END
                )
                RETURNING * INTO v_record;
        WHEN 'observe_staging' THEN
            UPDATE public.managed_original_creations SET source = p_facts->'source'
            WHERE id = p_creation AND status = 'preparing' AND origin = 'direct' AND source IS NULL
                RETURNING * INTO v_record;
        WHEN 'prepare' THEN
            UPDATE public.managed_original_creations
            SET status = 'prepared', frozen_at = (p_facts->>'frozen_at')::timestamptz,
                sha256 = p_facts->>'sha256', md5 = p_facts->>'md5', size_bytes = (p_facts->>'size_bytes')::bigint,
                prepared = p_facts->'prepared'
            WHERE id = p_creation AND status = 'preparing' RETURNING * INTO v_record;
        WHEN 'write_intent' THEN
            UPDATE public.managed_original_creations
            SET status = 'writing', write_started_at = (p_facts->>'write_started_at')::timestamptz
            WHERE id = p_creation AND status = 'prepared' RETURNING * INTO v_record;
        WHEN 'acknowledge' THEN
            UPDATE public.managed_original_creations
            SET status = 'written', acknowledged_at = (p_facts->>'acknowledged_at')::timestamptz,
                receipt = p_facts->'receipt', etag = p_facts->>'etag',
                version_id = p_facts->>'version_id', request_id = p_facts->>'request_id'
            WHERE id = p_creation AND status = 'writing' RETURNING * INTO v_record;
        WHEN 'readback' THEN
            UPDATE public.managed_original_creations
            SET verification = p_facts->'verification', readback_at = (p_facts->>'readback_at')::timestamptz
            WHERE id = p_creation AND status = 'written' AND verification IS NULL
                RETURNING * INTO v_record;
        WHEN 'settle_verified' THEN
            UPDATE public.managed_original_creations
            SET status = 'verified', settled_at = (p_facts->>'settled_at')::timestamptz,
                completed_at = (p_facts->>'completed_at')::timestamptz
            WHERE id = p_creation AND status = 'written' AND verification IS NOT NULL
                RETURNING * INTO v_record;
        WHEN 'settle_failure' THEN
            UPDATE public.managed_original_creations
            SET status = p_facts->>'status', error_code = p_facts->>'error_code',
                failure_evidence = p_facts->'failure_evidence', settled_at = (p_facts->>'settled_at')::timestamptz,
                completed_at = (p_facts->>'completed_at')::timestamptz
            WHERE id = p_creation AND status = p_expected
                AND status IN ('allocated','preparing','prepared','writing','written')
                AND p_facts->>'status' IN ('failed','conflict','unknown')
                RETURNING * INTO v_record;
        ELSE
            RAISE EXCEPTION 'Invalid original receipt step' USING ERRCODE = '22023';
    END CASE;
    IF FOUND THEN RETURN NEXT v_record; END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.protect_original_group(p_bucket text, p_key text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT pg_try_advisory_xact_lock_shared(
        hashtextextended(yoao_assets_v1._managed_reclaim_lock_identity(p_bucket, p_key), 0)
    ) THEN RETURN 'busy'; END IF;
    IF EXISTS (SELECT 1 FROM public.managed_original_reclaim_claims c
        WHERE c.bucket = p_bucket AND c.object_key = p_key AND c.state <> 'released'
    ) THEN RETURN 'claimed'; END IF;
    RETURN 'ok';
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.attach_verified_original(p_item uuid, p_creation uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations;
    v_asset public.dataset_assets;
    v_fence text;
BEGIN
    SELECT c.* INTO v_creation FROM public.managed_original_creations c
        WHERE c.id = p_creation AND c.owner_kind = 'dataset' AND c.status = 'verified';
    IF NOT FOUND THEN RETURN 'mismatch'; END IF;
    SELECT a.* INTO v_asset FROM public.dataset_assets a WHERE a.dataset_item_id = p_item FOR UPDATE;
    IF NOT FOUND OR v_asset.verification_state <> 'verified' OR v_asset.checksum_algorithm <> 'sha256'
        OR v_asset.checksum_value IS DISTINCT FROM v_creation.sha256
        OR v_asset.size_bytes IS DISTINCT FROM v_creation.size_bytes
        OR NOT EXISTS (SELECT 1 FROM public.dataset_items i WHERE i.id = p_item
            AND i.dataset_id = v_creation.dataset_id AND i.file_path = v_creation.object_key)
    THEN RETURN 'mismatch'; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.dataset_asset_locations l
        WHERE l.asset_id = v_asset.id AND l.location_id = v_creation.location_id
            AND l.bucket = v_creation.bucket AND l.object_key = v_creation.object_key
            AND l.role = 'managed_original' AND (l.creation_id IS NULL OR l.creation_id = p_creation)
            AND l.storage_ref IS NOT DISTINCT FROM CASE WHEN v_creation.configuration->'schema' = '2'::jsonb
                THEN nullif(v_creation.configuration->'original_ref','null'::jsonb) END
    ) THEN RETURN 'mismatch'; END IF;
    v_fence := yoao_assets_v1.protect_original_group(v_creation.bucket, v_creation.object_key);
    IF v_fence <> 'ok' THEN RETURN v_fence; END IF;
    UPDATE public.dataset_asset_locations l SET creation_id = p_creation
        WHERE l.asset_id = v_asset.id AND l.location_id = v_creation.location_id
            AND l.bucket = v_creation.bucket AND l.object_key = v_creation.object_key
            AND l.role = 'managed_original' AND (l.creation_id IS NULL OR l.creation_id = p_creation)
            AND l.storage_ref IS NOT DISTINCT FROM CASE WHEN v_creation.configuration->'schema' = '2'::jsonb
                THEN nullif(v_creation.configuration->'original_ref','null'::jsonb) END;
    RETURN CASE WHEN FOUND THEN 'ok' ELSE 'mismatch' END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.local_original_receipt(uuid),
    yoao_assets_v1.allocate_local_original(jsonb),
    yoao_assets_v1.record_local_original_step(uuid,text,text,jsonb),
    yoao_assets_v1.protect_original_group(text,text),
    yoao_assets_v1.attach_verified_original(uuid,uuid)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.original_reclaim_task_evidence(p_key text)
RETURNS TABLE(id uuid, dataset_item_id uuid, evidence jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT t.id, t.dataset_item_id, to_jsonb(t) FROM public.tasks t WHERE t.file_path = p_key
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.original_reclaim_task_evidence(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_registry_snapshot(p_location uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT jsonb_build_object(
        'location', (SELECT to_jsonb(l) FROM public.storage_locations l WHERE l.id = p_location),
        'bindings', (SELECT coalesce(jsonb_agg(to_jsonb(b) ORDER BY b.purpose), '[]'::jsonb)
            FROM public.storage_bindings b))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.original_registry_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_original_registry_snapshot()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_assets_v1.original_registry_snapshot('4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.legacy_original_registry_snapshot() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.observe_original_reclamation(
    p_mode text, p_location_id uuid, p_datasets_bucket text, p_annotations_bucket text,
    p_storage_identity text, p_location_config jsonb,
    p_retirement_id uuid, p_reclaim_attempt_id uuid, p_limit integer, p_offset integer
)
RETURNS TABLE(measured_at timestamptz, configuration_fingerprint jsonb, counts jsonb, items jsonb,
    observation jsonb, unknown jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
WITH clock AS (
    SELECT statement_timestamp() AS measured_at
),
patterns AS (
    SELECT '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AS receipt_uuid,
        '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$' AS receipt_timestamp
),
retirement_scope AS MATERIALIZED (
    SELECT r.* FROM public.managed_original_retirements r
    WHERE p_mode = 'preview' OR (p_mode = 'reclaim' AND r.id = p_retirement_id)
    UNION ALL
    SELECT x.* FROM (
        SELECT DISTINCT ON (r.bucket, r.object_key) r.* FROM public.managed_original_retirements r
        WHERE p_mode = 'reconcile' ORDER BY r.bucket, r.object_key, r.id
    ) x
),
locator_peers AS NOT MATERIALIZED (
    SELECT p.* FROM public.managed_original_retirements p
    WHERE p_mode <> 'reclaim'
    UNION ALL
    SELECT p.* FROM retirement_scope r JOIN public.managed_original_retirements p
       ON md5(p.bucket) = md5(r.bucket) AND md5(p.object_key) = md5(r.object_key)
          AND p.bucket = r.bucket AND p.object_key = r.object_key
    WHERE p_mode = 'reclaim'
),
active_declared_holds AS (
    SELECT h.retirement_id,
        count(*) FILTER (WHERE h.kind = 'backup') AS backup_holds,
        count(*) FILTER (WHERE h.kind = 'migration') AS migration_holds
    FROM public.managed_original_retention_holds h
    JOIN locator_peers r ON r.id = h.retirement_id
    WHERE h.released_at IS NULL
    GROUP BY h.retirement_id
), peer_protection AS MATERIALIZED (
    SELECT r.bucket, r.object_key,
        count(*) AS peer_assertions,
        bool_or(r.retain_until IS NULL) AS indefinite_retention,
        CASE WHEN bool_or(r.retain_until IS NULL) THEN NULL
            ELSE max(r.retain_until) END AS effective_retain_until,
        COALESCE(sum(h.backup_holds), 0) AS declared_backup_holds,
        COALESCE(sum(h.migration_holds), 0) AS declared_migration_holds
    FROM locator_peers r
    LEFT JOIN active_declared_holds h ON h.retirement_id = r.id
    GROUP BY r.bucket, r.object_key
),
training_history AS MATERIALIZED (SELECT * FROM yoao_learning_v1.training_original_history()),
learning_history AS MATERIALIZED (SELECT * FROM yoao_learning_v1.active_learning_original_history()),
learning_decisions AS MATERIALIZED (SELECT * FROM yoao_learning_v1.active_learning_original_decisions()),
training_documents AS MATERIALIZED (
    SELECT id, snapshot,
        COALESCE(jsonb_typeof(snapshot) = 'object'
        AND jsonb_typeof(snapshot->'samples') = 'array'
        AND (NOT snapshot ? 'managed_original_contract' OR snapshot->'managed_original_contract' = '1'::jsonb)
        AND (NOT snapshot ? 'source_lineage_contract'
            OR snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb)
        AND (NOT snapshot ? 'asset_source_contract'
            OR snapshot->'asset_source_contract' = '{"schema": 1,"reader":"verified-asset-reference-1","freeze":"training-copy"}'::jsonb)
        AND (
            (snapshot->'schema' = '1'::jsonb
             AND (NOT snapshot ? 'task' OR snapshot->'task' = '"image-detection"'::jsonb)
             AND NOT snapshot ? 'geometry' AND NOT snapshot ? 'bridge'
             AND (NOT snapshot ? 'format' OR snapshot->'format' = '"ultralytics-detection"'::jsonb))
            OR (snapshot->'schema' = '2'::jsonb
                AND snapshot @> '{"task":"image-instance-segmentation", "geometry":"normalized-simple-polygon", "bridge":"ultralytics-simple-polygon-1"}'::jsonb
                AND (NOT snapshot ? 'format' OR snapshot->'format' = '"ultralytics-segmentation"'::jsonb))
            OR (snapshot->'schema' = '3'::jsonb
                AND snapshot @> '{"media_type":"image", "format":"ultralytics-yolo"}'::jsonb
                AND ((snapshot @> '{"task":"detect", "geometry":"normalized-bbox", "bridge":"ultralytics-bbox-1"}'::jsonb)
                    OR (snapshot @> '{"task":"segment", "geometry":"normalized-simple-polygon", "bridge":"ultralytics-simple-polygon-1"}'::jsonb)))
            OR (snapshot->'schema' = '4'::jsonb
                AND snapshot @> '{"task":"segment", "media_type":"image", "format":"aap-instance-rle", "geometry":"instance-mask", "bridge":"ultralytics-instance-rle-1"}'::jsonb)
        ), false) AS supported
    FROM training_history
), training_samples AS MATERIALIZED (
    SELECT d.id, d.snapshot, s.value AS sample,
        COALESCE(jsonb_typeof(s.value) = 'object' AND (
            (d.snapshot->'managed_original_contract' = '1'::jsonb AND COALESCE(jsonb_typeof(s.value->'original') = 'object'
        AND s.value->'original'->'schema' = '1'::jsonb
        AND s.value->'original'->>'owner_kind' IN ('dataset_asset','task_original')
        AND (s.value->'original') - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{}'::jsonb
        AND jsonb_typeof(s.value->'original'->'content_id') = 'string'
        AND s.value->'original'->>'content_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(s.value->'original'->'location_id') = 'string'
        AND s.value->'original'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(s.value->'original'->'sha256') = 'string'
        AND s.value->'original'->>'sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(s.value->'original'->'size_bytes') = 'number'
        AND s.value->'original'->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            (s.value->'original'->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = s.value->'original'->>'content_id')
                OR EXISTS (SELECT 1 FROM public.managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = s.value->'original'->>'content_id')))
            OR (s.value->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_task WHERE CAST(receipt_task.id AS text) = s.value->'original'->>'content_id'))
        ), false))
            OR (jsonb_typeof(s.value->'source_bucket') = 'string'
             AND length(s.value->>'source_bucket') > 0
             AND jsonb_typeof(s.value->'source_key') = 'string'
             AND length(s.value->>'source_key') > 0
             AND left(s.value->>'source_key', 18) <> '__aap_reference__/'
             AND NOT s.value ? 'asset_reference' AND NOT s.value ? 'source_lineage'
             AND NOT s.value ? 'original' AND NOT d.snapshot ? 'managed_original_contract')
            OR (
                NOT s.value ? 'original' AND jsonb_typeof(s.value->'asset_reference') = 'object'
                AND s.value->'asset_reference'->'schema' = '1'::jsonb
                AND (s.value->'asset_reference') - ARRAY[
                    'schema', 'dataset_id', 'dataset_item_id', 'asset_id', 'sha256', 'size_bytes'
                ] = '{}'::jsonb
                AND jsonb_typeof(s.value->'asset_reference'->'asset_id') = 'string'
                AND s.value->'asset_reference'->>'asset_id' ~ (SELECT receipt_uuid FROM patterns)
                AND jsonb_typeof(s.value->'asset_reference'->'dataset_id') = 'string'
                AND s.value->'asset_reference'->>'dataset_id' ~ (SELECT receipt_uuid FROM patterns)
                AND jsonb_typeof(s.value->'asset_reference'->'dataset_item_id') = 'string'
                AND s.value->'asset_reference'->>'dataset_item_id' ~ (SELECT receipt_uuid FROM patterns)
                AND jsonb_typeof(s.value->'asset_reference'->'sha256') = 'string'
                AND s.value->'asset_reference'->>'sha256' ~ '^[0-9a-f]{64}$'
                AND jsonb_typeof(s.value->'asset_reference'->'size_bytes') = 'number'
                AND s.value->'asset_reference'->>'size_bytes' ~ '^[0-9]+$'
                AND s.value->>'source_key' = '__aap_reference__/'
                    || (s.value->'asset_reference'->>'dataset_item_id') || '/'
                    || (s.value->'asset_reference'->>'sha256')
                AND (NOT s.value ? 'source_bucket')
                AND (
                    EXISTS (SELECT 1 FROM public.managed_original_retirements retained_asset
                        WHERE CAST(retained_asset.asset_id AS text) = s.value->'asset_reference'->>'asset_id')
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations saved_asset
                        WHERE CAST(saved_asset.asset_id AS text) = s.value->'asset_reference'->>'asset_id')
                    OR EXISTS (SELECT 1 FROM public.dataset_assets saved_asset
                        JOIN public.dataset_references saved_reference ON saved_reference.dataset_item_id = saved_asset.dataset_item_id
                        JOIN public.dataset_scan_entries saved_entry ON saved_entry.run_id = saved_reference.run_id
                            AND saved_entry.path_digest = saved_reference.path_digest
                        JOIN public.dataset_scan_runs saved_run ON saved_run.id = saved_entry.run_id
                        JOIN public.dataset_sources saved_source ON saved_source.id = saved_run.source_id
                        JOIN public.storage_locations saved_location ON saved_location.id = saved_source.location_id
                        WHERE CAST(saved_asset.id AS text) = s.value->'asset_reference'->>'asset_id'
                            AND jsonb_typeof(saved_entry.observed) = 'object'
                            AND (saved_location.kind IN ('filesystem','sftp') OR (
                                saved_entry.observed->>'kind' = 's3'
                                AND jsonb_typeof(saved_entry.observed->'bucket') = 'string'
                                AND length(saved_entry.observed->>'bucket') > 0
                                AND jsonb_typeof(saved_entry.observed->'key') = 'string'
                                AND length(saved_entry.observed->>'key') > 0)))
                    OR EXISTS (SELECT 1 FROM public.dataset_scan_entries saved_entry
                        JOIN public.dataset_scan_runs saved_run ON saved_run.id = saved_entry.run_id
                        JOIN public.dataset_sources saved_source ON saved_source.id = saved_run.source_id
                        JOIN public.storage_locations saved_location ON saved_location.id = saved_source.location_id
                        WHERE CAST(saved_entry.run_id AS text) = s.value->'source_lineage'->>'scan_run_id'
                            AND saved_entry.path_digest = s.value->'source_lineage'->>'path_digest'
                            AND jsonb_typeof(saved_entry.observed) = 'object'
                            AND (saved_location.kind IN ('filesystem','sftp') OR (
                                saved_entry.observed->>'kind' = 's3'
                                AND jsonb_typeof(saved_entry.observed->'bucket') = 'string'
                                AND length(saved_entry.observed->>'bucket') > 0
                                AND jsonb_typeof(saved_entry.observed->'key') = 'string'
                                AND length(saved_entry.observed->>'key') > 0)))
                )
                AND ((NOT s.value ? 'source_lineage' AND NOT d.snapshot ? 'source_lineage_contract') OR (
                    d.snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb
                    AND jsonb_typeof(s.value->'source_lineage') = 'object'
                    AND EXISTS (SELECT 1 FROM public.dataset_scan_entries lineage_entry
                        JOIN public.dataset_scan_runs lineage_run ON lineage_run.id = lineage_entry.run_id
                        JOIN public.dataset_sources lineage_source ON lineage_source.id = lineage_run.source_id
                        JOIN public.storage_locations lineage_location ON lineage_location.id = lineage_source.location_id
                        WHERE CAST(lineage_entry.run_id AS text) = s.value->'source_lineage'->>'scan_run_id'
                            AND lineage_entry.path_digest = s.value->'source_lineage'->>'path_digest'
                            AND CAST(lineage_source.id AS text) = s.value->'source_lineage'->>'source_id'
                            AND CAST(lineage_location.id AS text) = s.value->'source_lineage'->>'location_id'
                            AND lineage_entry.verified_content->>'sha256' = s.value->'asset_reference'->>'sha256'
                            AND lineage_entry.verified_content->>'size_bytes' = s.value->'asset_reference'->>'size_bytes'
                            AND (lineage_location.kind IN ('filesystem','sftp') OR (
                                lineage_location.kind = 's3'
                                AND lineage_run.snapshot->'anchor'->>'kind' = 's3'
                                AND lineage_entry.observed->>'kind' = 's3'
                                AND lineage_entry.observed->>'bucket' = lineage_run.snapshot->'anchor'->>'bucket'
                                AND jsonb_typeof(lineage_entry.observed->'key') = 'string'
                                AND lineage_entry.observed->>'key' =
                                    (lineage_run.snapshot->'anchor'->>'prefix')
                                    || CASE WHEN lineage_run.snapshot->'config'->>'relative_path' = '' OR right(lineage_run.snapshot->'config'->>'relative_path', 1) = '/' THEN lineage_run.snapshot->'config'->>'relative_path'
                                        ELSE (lineage_run.snapshot->'config'->>'relative_path') || '/' END
                                    || lineage_entry.relative_path)))
                    AND jsonb_typeof(s.value->'source_lineage'->'location_id') = 'string'
                    AND s.value->'source_lineage'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
                    AND jsonb_typeof(s.value->'source_lineage'->'source_id') = 'string'
                    AND s.value->'source_lineage'->>'source_id' ~ (SELECT receipt_uuid FROM patterns)
                    AND s.value->'source_lineage'->>'scan_run_id' ~ (SELECT receipt_uuid FROM patterns)
                    AND jsonb_typeof(s.value->'source_lineage'->'scan_run_id') = 'string'
                    AND s.value->'source_lineage'->>'path_digest' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(s.value->'source_lineage'->'path_digest') = 'string'
                    AND s.value->'source_lineage'->>'source_config_revision' ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(s.value->'source_lineage'->'source_config_revision') = 'number'
                    AND s.value->'source_lineage'->>'location_config_revision' ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(s.value->'source_lineage'->'location_config_revision') = 'number'
                    AND (s.value->'source_lineage') - ARRAY[
                        'source_id', 'location_id', 'scan_run_id', 'path_digest',
                        'source_config_revision', 'location_config_revision',
                        'accepted_revision_id', 'revision_number', 'previous_revision_id',
                        'accepted_at', 'scan_completed_at', 'content_verified_at'
                    ] = '{}'::jsonb
                    AND (s.value->'source_lineage'->'accepted_revision_id' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'accepted_revision_id') = 'string'
                            AND s.value->'source_lineage'->>'accepted_revision_id' ~ (SELECT receipt_uuid FROM patterns)))
                    AND (s.value->'source_lineage'->'previous_revision_id' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'previous_revision_id') = 'string'
                            AND s.value->'source_lineage'->>'previous_revision_id' ~ (SELECT receipt_uuid FROM patterns)))
                    AND (s.value->'source_lineage'->'revision_number' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'revision_number') = 'number'
                            AND s.value->'source_lineage'->>'revision_number' ~ '^[1-9][0-9]*$'))
                    AND (s.value->'source_lineage'->'accepted_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'accepted_at') = 'string'
                            AND s.value->'source_lineage'->>'accepted_at' ~ (SELECT receipt_timestamp FROM patterns)))
                    AND (s.value->'source_lineage'->'scan_completed_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'scan_completed_at') = 'string'
                            AND s.value->'source_lineage'->>'scan_completed_at' ~ (SELECT receipt_timestamp FROM patterns)))
                    AND (s.value->'source_lineage'->'content_verified_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'content_verified_at') = 'string'
                            AND s.value->'source_lineage'->>'content_verified_at' ~ (SELECT receipt_timestamp FROM patterns)))
                ))
            )
        ), false) AS supported
    FROM training_documents d
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(d.snapshot->'samples') = 'array'
            THEN d.snapshot->'samples' ELSE '[]'::jsonb END
    ) s(value)
), training_unknown AS (
    SELECT count(*) AS count FROM training_documents d
    WHERE NOT d.supported OR EXISTS (
        SELECT 1 FROM training_samples s WHERE s.id = d.id AND NOT s.supported
    )
), learning_sources AS MATERIALIZED (
    SELECT a.id, p.value->'snapshot' AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND jsonb_typeof(p.value->'snapshot') = 'object'
            AND NOT (p.value->'snapshot') ? 'asset_reference'
            AND NOT (p.value->'snapshot') ? 'source_lineage'
            AND (
            (a.spec->'original_contract' = '1'::jsonb AND COALESCE(jsonb_typeof(p.value->'snapshot'->'original') = 'object'
        AND p.value->'snapshot'->'original'->'schema' = '1'::jsonb
        AND p.value->'snapshot'->'original'->>'owner_kind' IN ('dataset_asset','task_original')
        AND (p.value->'snapshot'->'original') - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{}'::jsonb
        AND jsonb_typeof(p.value->'snapshot'->'original'->'content_id') = 'string'
        AND p.value->'snapshot'->'original'->>'content_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'snapshot'->'original'->'location_id') = 'string'
        AND p.value->'snapshot'->'original'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'snapshot'->'original'->'sha256') = 'string'
        AND p.value->'snapshot'->'original'->>'sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(p.value->'snapshot'->'original'->'size_bytes') = 'number'
        AND p.value->'snapshot'->'original'->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            (p.value->'snapshot'->'original'->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = p.value->'snapshot'->'original'->>'content_id')
                OR EXISTS (SELECT 1 FROM public.managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = p.value->'snapshot'->'original'->>'content_id')))
            OR (p.value->'snapshot'->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_task WHERE CAST(receipt_task.id AS text) = p.value->'snapshot'->'original'->>'content_id'))
        ), false))
            OR (NOT a.spec ? 'original_contract'
            AND jsonb_typeof(p.value->'snapshot'->'source_bucket') = 'string'
            AND length(p.value->'snapshot'->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'snapshot'->'source_key') = 'string'
            AND length(p.value->'snapshot'->>'source_key') > 0)), false) AS supported
    FROM learning_history a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.spec->'pool') = 'array'
            THEN a.spec->'pool' ELSE '[]'::jsonb END
    ) p(value)
), decision_sources AS MATERIALIZED (
    SELECT a.id, p.value AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND NOT p.value ? 'asset_reference' AND NOT p.value ? 'source_lineage'
            AND (
            (p.value ? 'original' AND COALESCE(jsonb_typeof(p.value->'original') = 'object'
        AND p.value->'original'->'schema' = '1'::jsonb
        AND p.value->'original'->>'owner_kind' IN ('dataset_asset','task_original')
        AND (p.value->'original') - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{}'::jsonb
        AND jsonb_typeof(p.value->'original'->'content_id') = 'string'
        AND p.value->'original'->>'content_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'original'->'location_id') = 'string'
        AND p.value->'original'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'original'->'sha256') = 'string'
        AND p.value->'original'->>'sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(p.value->'original'->'size_bytes') = 'number'
        AND p.value->'original'->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            (p.value->'original'->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = p.value->'original'->>'content_id')
                OR EXISTS (SELECT 1 FROM public.managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = p.value->'original'->>'content_id')))
            OR (p.value->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_task WHERE CAST(receipt_task.id AS text) = p.value->'original'->>'content_id'))
        ), false))
            OR (NOT p.value ? 'original' AND jsonb_typeof(p.value->'source_bucket') = 'string'
            AND length(p.value->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'source_key') = 'string'
            AND length(p.value->>'source_key') > 0)), false) AS supported
    FROM learning_decisions a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.snapshot->'source_snapshots') = 'array'
            THEN a.snapshot->'source_snapshots' ELSE '[]'::jsonb END
    ) p(value)
), learning_unknown AS (
    SELECT count(*) AS count FROM learning_history a
    WHERE NOT COALESCE(jsonb_typeof(a.spec) = 'object'
        AND a.spec->'schema' IN ('1'::jsonb, '2'::jsonb)
        AND (NOT a.spec ? 'original_contract' OR a.spec->'original_contract' = '1'::jsonb)
        AND jsonb_typeof(a.spec->'pool') = 'array', false)
        OR EXISTS (SELECT 1 FROM learning_sources s WHERE s.id = a.id AND NOT s.supported)
        OR EXISTS (SELECT 1 FROM learning_decisions d
            WHERE d.round_id = a.id AND (
                NOT COALESCE(jsonb_typeof(d.snapshot->'source_snapshots') = 'array', false)
                OR EXISTS (SELECT 1 FROM decision_sources s WHERE s.id = d.id AND NOT s.supported)
            ))
),
evidence AS MATERIALIZED (
    SELECT r.*, clock.measured_at,
        p.peer_assertions, p.indefinite_retention, p.effective_retain_until,
        p.declared_backup_holds, p.declared_migration_holds,
        CASE WHEN r.identity_state <> 'verified_binding' OR r.storage_identity IS NULL
            OR r.binding_generation IS NULL OR r.location_config_revision IS NULL
            THEN 'unknown'
        WHEN EXISTS (SELECT 1 FROM public.managed_original_creations creation
            WHERE creation.id = r.creation_id AND creation.status = 'verified'
                AND creation.storage_identity = r.storage_identity
                AND creation.bucket = r.bucket AND creation.object_key = r.object_key
                AND creation.location_id = r.location_id
                AND creation.configuration->'original_ref'->>'location_id' = CAST(r.location_id AS text)
                AND creation.configuration->'original_ref'->>'bucket' = r.bucket
                AND creation.configuration->'original_ref'->>'object_key' = r.object_key)
            THEN 'current'
        WHEN r.location_id = CAST(p_location_id AS uuid)
            AND r.storage_identity = p_storage_identity AND r.bucket = p_datasets_bucket
            AND b.location_id = r.location_id AND b.bucket = r.bucket
            AND b.generation = r.binding_generation
            AND l.kind = 's3' AND l.config = CAST(p_location_config AS jsonb)
            AND l.config_revision = r.location_config_revision
            THEN 'current' ELSE 'mismatch' END AS current_identity,
        ((SELECT count(*) FROM public.dataset_asset_locations x
            WHERE x.bucket = r.bucket AND x.object_key = r.object_key)
         + (SELECT count(*) FROM public.dataset_auxiliary_originals x
            WHERE x.bucket = r.bucket AND x.object_key = r.object_key)) AS asset_locations,
        (SELECT count(*) FROM public.dataset_items i
            WHERE r.bucket = p_datasets_bucket AND i.file_path = r.object_key) AS dataset_items,
        (SELECT count(*) FROM yoao_labeling_v1.original_reclaim_task_evidence(r.object_key) t
            WHERE r.bucket = CASE WHEN t.dataset_item_id IS NULL
                THEN p_annotations_bucket ELSE p_datasets_bucket END) AS tasks,
        (SELECT count(*) FROM yoao_labeling_v1.original_reclaim_task_evidence(r.object_key) t
            WHERE r.bucket <> CASE WHEN t.dataset_item_id IS NULL
                THEN p_annotations_bucket ELSE p_datasets_bucket END) AS ambiguous_legacy_tasks,
        (SELECT count(*) FROM yoao_labeling_v1.task_dataset_item_link_rows k
            JOIN public.dataset_items i ON i.id = k.dataset_item_id
            WHERE (r.bucket = p_datasets_bucket AND i.file_path = r.object_key)
                OR EXISTS (SELECT 1 FROM public.dataset_assets a
                    JOIN public.dataset_asset_locations x ON x.asset_id = a.id
                    WHERE a.dataset_item_id = i.id
                        AND x.bucket = r.bucket AND x.object_key = r.object_key)) AS task_dataset_links,
        (SELECT count(DISTINCT s.id) FROM training_samples s WHERE
            (s.sample->>'source_bucket' = r.bucket AND s.sample->>'source_key' = r.object_key)
            OR (s.sample->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.sample->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.sample->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.sample->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.sample->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))
            OR EXISTS (SELECT 1 FROM public.dataset_scan_entries alias_entry
                WHERE CAST(alias_entry.run_id AS text) = s.sample->'source_lineage'->>'scan_run_id'
                    AND alias_entry.path_digest = s.sample->'source_lineage'->>'path_digest'
                    AND alias_entry.observed->>'kind' = 's3'
                    AND alias_entry.observed->>'bucket' = r.bucket
                    AND alias_entry.observed->>'key' = r.object_key)

            OR EXISTS (SELECT 1 FROM public.managed_original_retirements peer
                WHERE md5(peer.bucket) = md5(r.bucket)
                    AND md5(peer.object_key) = md5(r.object_key)
                    AND peer.bucket = r.bucket
                    AND peer.object_key = r.object_key
                    AND (s.sample->'asset_reference'->>'asset_id' = CAST(peer.asset_id AS text)
                        OR s.sample->'asset_reference'->>'dataset_item_id' = CAST(peer.dataset_item_id AS text)))
        ) AS training_versions,
        ((SELECT count(DISTINCT s.id) FROM learning_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR (s.snapshot->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.snapshot->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.snapshot->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.snapshot->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))

            OR EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->>'dataset_item_id' = CAST(original_peer.dataset_item_id AS text)))
         + (SELECT count(DISTINCT s.id) FROM decision_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR (s.snapshot->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.snapshot->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.snapshot->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.snapshot->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))
            OR EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->>'dataset_item_id' = CAST(original_peer.dataset_item_id AS text)))
         + yoao_learning_v1.active_learning_pool_item_count(ARRAY(
            SELECT original_peer.dataset_item_id FROM public.managed_original_retirements original_peer
            WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key))
         + yoao_learning_v1.active_learning_candidate_item_count(ARRAY(
            SELECT original_peer.dataset_item_id FROM public.managed_original_retirements original_peer
            WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key))
        ) AS active_learning_roots,
        u.count AS unknown_training_versions,
        au.count AS unknown_active_learning_rounds
    FROM retirement_scope r
    JOIN peer_protection p ON md5(p.bucket) = md5(r.bucket)
        AND md5(p.object_key) = md5(r.object_key)
        AND p.bucket = r.bucket AND p.object_key = r.object_key
    CROSS JOIN clock CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
    LEFT JOIN public.storage_bindings b ON b.purpose = 'datasets'
    LEFT JOIN public.storage_locations l ON l.id = r.location_id
),
classified AS MATERIALIZED (
    SELECT e.*,
        (asset_locations + dataset_items + tasks + task_dataset_links
            + training_versions + active_learning_roots
            + declared_backup_holds + declared_migration_holds > 0) AS protected,
        (indefinite_retention OR effective_retain_until > measured_at) AS retained,
        (NOT indefinite_retention AND effective_retain_until <= measured_at
            AND declared_backup_holds = 0 AND declared_migration_holds = 0
            AND current_identity = 'current'
            AND asset_verification_state = 'verified' AND sha256 IS NOT NULL
            AND sha256 ~ '^[0-9a-f]{64}$'
            AND size_bytes IS NOT NULL AND reference_state = 'present'
            AND verified_at IS NOT NULL
            AND asset_locations + dataset_items + tasks + task_dataset_links
                + training_versions + active_learning_roots = 0
            AND ambiguous_legacy_tasks = 0 AND unknown_training_versions = 0
            AND unknown_active_learning_rounds = 0) AS policy_candidate
    FROM evidence e
), page AS (
    SELECT * FROM classified ORDER BY retired_at, id LIMIT LEAST(p_limit, 100) OFFSET p_offset
), totals AS (
    SELECT count(*) AS total_assertions,
        count(*) FILTER (WHERE protected) AS protected_assertions,
        count(*) FILTER (WHERE NOT protected AND retained) AS retained_assertions,
        count(*) FILTER (WHERE NOT protected AND NOT retained) AS blocked_assertions,
        count(*) FILTER (WHERE policy_candidate) AS policy_candidate_assertions,
        count(*) FILTER (WHERE size_bytes IS NULL) AS unknown_size_assertions,
        COALESCE(sum(size_bytes), 0) AS known_asserted_bytes
    FROM classified
),
own_operation AS (
    SELECT a.operation_id FROM public.managed_original_reclaim_attempts a
    JOIN public.managed_original_reclaim_claims c ON c.current_attempt_id = a.id
    JOIN evidence r ON r.id = a.retirement_id AND r.creation_id = a.creation_id
    JOIN LATERAL yoao_jobs_v1.read_job(a.async_job_id) job ON true
    JOIN public.storage_operations op ON op.id = a.operation_id
    WHERE a.id = CAST(p_reclaim_attempt_id AS uuid) AND a.status = 'running'
        AND job.status = 'running' AND job.kind = 'managed_original_reclaim'
        AND job.celery_task_id = a.task_id AND job.user_id = a.requested_by
        AND job.payload->>'reclaim_id' = CAST(a.id AS text)
        AND job.payload->>'retirement_id' = CAST(r.id AS text)
        AND op.purpose = 'managed_original_reclaim' AND op.location_id = r.location_id
        AND op.revision = a.maintenance_revision
        AND NOT a.physical_settled AND c.state IN ('claimed','tombstone')
        AND c.creation_id = a.creation_id AND c.version_id = a.version_id
        AND c.bucket = r.bucket AND c.object_key = r.object_key
        AND a.target->>'location_id' = CAST(r.location_id AS text)
        AND a.target->>'bucket' = r.bucket AND a.target->>'object_key' = r.object_key
        AND a.operation_id IS NOT NULL
),
aliases AS MATERIALIZED (
    SELECT d.dataset_item_id, d.run_id, d.path_digest, e.observed,
        run.snapshot, source.location_id, location.kind AS location_kind
    FROM public.dataset_references d
    JOIN public.dataset_scan_entries e ON e.run_id = d.run_id AND e.path_digest = d.path_digest
    JOIN public.dataset_scan_runs run ON run.id = e.run_id
    JOIN public.dataset_sources source ON source.id = d.source_id
    JOIN public.storage_locations location ON location.id = source.location_id
), unknown_aliases AS (
    SELECT count(*) AS count FROM aliases a WHERE a.location_kind = 's3'
        AND NOT COALESCE(a.observed->>'kind' = 's3'
            AND jsonb_typeof(a.observed->'bucket') = 'string'
            AND length(a.observed->>'bucket') > 0
            AND jsonb_typeof(a.observed->'key') = 'string'
            AND length(a.observed->>'key') > 0, false)
),
unknown_operations AS (
    SELECT count(*) AS count FROM public.storage_operations o
    JOIN public.storage_locations sl ON sl.id = o.location_id
    LEFT JOIN public.storage_connections sc ON sc.id = sl.connection_id
    WHERE sl.kind = 's3' AND sl.id <> CAST(p_location_id AS uuid)
        AND NOT COALESCE(jsonb_typeof(sc.config->'bucket') = 'string'
            AND length(sc.config->>'bucket') > 0, false)
        AND NOT EXISTS (SELECT 1 FROM own_operation own WHERE own.operation_id = o.id)
),
groups AS (
    SELECT r.*, to_jsonb(anchor) AS anchor, to_jsonb(creation) AS creation,
        jsonb_build_object('location', to_jsonb(location),
            'connection', CASE WHEN connection.id IS NOT NULL THEN jsonb_build_object(
                'id', connection.id, 'name', connection.name, 'revision', connection.revision,
                'kind', connection.kind, 'config', connection.config, 'scope', connection.scope,
                'project_id', connection.project_id, 'created_by', connection.created_by,
                'created_at', connection.created_at, 'updated_at', connection.updated_at) END) AS configuration,
        COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id)
            FROM locator_peers p
            WHERE p.bucket = r.bucket AND p.object_key = r.object_key), '[]'::jsonb) AS peers,
        COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY h.id)
            FROM public.managed_original_retention_holds h
            JOIN locator_peers p ON p.id = h.retirement_id
            WHERE p.bucket = r.bucket AND p.object_key = r.object_key), '[]'::jsonb) AS holds,
        COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM public.task_originals o
            WHERE o.bucket = r.bucket AND o.object_key = r.object_key), '[]'::jsonb) AS task_originals,
        COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.dataset_item_id) FROM aliases a
            WHERE a.observed->>'kind' = 's3' AND a.observed->>'bucket' = r.bucket
                AND a.observed->>'key' = r.object_key), '[]'::jsonb) AS reference_aliases,
        ua.count AS unknown_reference_aliases,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', i.async_job_id, 'status', i.status,
                'source', i.source, 'operation_id', i.operation_id) ORDER BY i.async_job_id)
            FROM public.storage_migration_inventories i
            WHERE (i.status IN ('pending','running') OR EXISTS (
                SELECT 1 FROM public.storage_operations o WHERE o.id = i.operation_id))
                AND EXISTS (SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(i.source->'purpose_buckets') = 'object' THEN i.source->'purpose_buckets' ELSE '{}'::jsonb END) b
                    WHERE b.value = r.bucket)), '[]'::jsonb) AS migration_inventories,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', p.id,
                'source', p.source, 'target', p.target, 'summary', p.summary,
                'delta_sequence', p.delta_sequence) ORDER BY p.id)
            FROM public.storage_migration_copies p
            WHERE (NOT COALESCE(p.summary->'copy_subset_complete' = 'true'::jsonb, false)
                OR EXISTS (SELECT 1 FROM public.storage_migration_copy_attempts a
                    WHERE a.copy_id = p.id AND (a.status IN ('pending','running')
                        OR EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id IN
                            (a.source_operation_id, a.target_operation_id)))))
                AND (p.target->>'bucket' = r.bucket OR EXISTS (
                    SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(p.source->'purpose_buckets') = 'object' THEN p.source->'purpose_buckets' ELSE '{}'::jsonb END) b
                    WHERE b.value = r.bucket))), '[]'::jsonb) AS migration_copies,
        COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id)
            FROM public.storage_operations o
            WHERE NOT EXISTS (SELECT 1 FROM own_operation own WHERE own.operation_id = o.id)
                AND (o.location_id = r.location_id OR EXISTS (
                    SELECT 1 FROM public.managed_original_retirements physical_peer
                    WHERE physical_peer.bucket = r.bucket AND physical_peer.object_key = r.object_key
                        AND physical_peer.location_id = o.location_id)
                OR EXISTS (
                    SELECT 1 FROM public.dataset_asset_locations al
                    WHERE al.location_id = o.location_id AND al.bucket = r.bucket AND al.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM public.task_originals t WHERE t.location_id = o.location_id
                    AND t.bucket = r.bucket AND t.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM public.managed_original_creations saved_creation
                    WHERE saved_creation.location_id = o.location_id
                        AND saved_creation.bucket = r.bucket AND saved_creation.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM aliases a WHERE a.location_id = o.location_id
                    AND a.observed->>'kind' = 's3' AND a.observed->>'bucket' = r.bucket
                    AND a.observed->>'key' = r.object_key)
                OR EXISTS (SELECT 1 FROM public.dataset_scan_runs saved_run
                    JOIN public.dataset_sources saved_source ON saved_source.id = saved_run.source_id
                    WHERE saved_source.location_id = o.location_id
                        AND saved_run.snapshot->'anchor'->>'kind' = 's3'
                        AND saved_run.snapshot->'anchor'->>'bucket' = r.bucket)
                OR EXISTS (SELECT 1 FROM public.storage_locations sl
                    LEFT JOIN public.storage_connections sc ON sc.id = sl.connection_id
                    WHERE sl.id = o.location_id AND sl.kind = 's3'
                        AND (sc.config->>'bucket' = r.bucket
                            OR sl.id = CAST(p_location_id AS uuid))))) , '[]'::jsonb) AS operations,
        uo.count AS unknown_operations,
        jsonb_build_object(
            'assets', (SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY a.asset_id, a.location_id, a.bucket), '[]'::jsonb)
                FROM public.dataset_asset_locations a WHERE a.bucket = r.bucket AND a.object_key = r.object_key),
            'auxiliary', (SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY a.id), '[]'::jsonb)
                FROM public.dataset_auxiliary_originals a WHERE a.bucket = r.bucket AND a.object_key = r.object_key),
            'items', (SELECT COALESCE(jsonb_agg(to_jsonb(i) ORDER BY i.id), '[]'::jsonb)
                FROM public.dataset_items i WHERE i.file_path = r.object_key),
            'tasks', (SELECT COALESCE(jsonb_agg(t.evidence ORDER BY t.id), '[]'::jsonb)
                FROM yoao_labeling_v1.original_reclaim_task_evidence(r.object_key) t),
            'training', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', v.id, 'snapshot', v.snapshot)::text), '' ORDER BY v.id), ''))
                FROM training_history v),
            'learning', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', a.id, 'spec', a.spec)::text), '' ORDER BY a.id), ''))
                FROM learning_history a),
            'decisions', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', a.id, 'snapshot', a.snapshot)::text), '' ORDER BY a.id), ''))
                FROM learning_decisions a)
        ) AS root_evidence
    FROM evidence r JOIN public.managed_original_retirements anchor ON anchor.id = r.id
    LEFT JOIN public.managed_original_creations creation ON creation.id = r.creation_id
    LEFT JOIN public.storage_locations location ON location.id = r.location_id
    LEFT JOIN public.storage_connections connection ON connection.id = location.connection_id
    CROSS JOIN unknown_aliases ua CROSS JOIN unknown_operations uo
),
grouped AS (
    SELECT bucket, object_key,
        bool_or(asset_locations + dataset_items + tasks + task_dataset_links
            + training_versions + active_learning_roots + ambiguous_legacy_tasks
            + jsonb_array_length(task_originals) + jsonb_array_length(reference_aliases)
            + jsonb_array_length(migration_inventories) + jsonb_array_length(migration_copies)
            + jsonb_array_length(operations) + declared_backup_holds + declared_migration_holds > 0) AS protected,
        bool_or(indefinite_retention OR effective_retain_until > measured_at) AS retained,
        bool_or(unknown_training_versions + unknown_active_learning_rounds
            + unknown_reference_aliases + unknown_operations > 0) AS blocked
    FROM groups GROUP BY bucket, object_key
)
SELECT p.measured_at, p.configuration_fingerprint, p.counts, p.items, NULL::jsonb, NULL::jsonb
FROM (
SELECT clock.measured_at, jsonb_build_object(
    'location', (SELECT to_jsonb(l) FROM public.storage_locations l
        WHERE l.id = CAST(p_location_id AS uuid)),
    'bindings', (SELECT COALESCE(jsonb_agg(to_jsonb(b) ORDER BY b.purpose), '[]'::jsonb)
        FROM public.storage_bindings b)
) AS configuration_fingerprint,
    to_jsonb(totals) || jsonb_build_object(
        'unknown_training_versions', u.count,
        'unknown_active_learning_rounds', au.count
    ) AS counts,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id', p.id, 'owner_type', p.owner_type, 'asset_id', p.asset_id,
        'auxiliary_original_id', p.auxiliary_original_id, 'dataset_id', p.dataset_id,
        'dataset_item_id', p.dataset_item_id, 'retired_at', p.retired_at,
        'policy_revision', p.policy_revision, 'retain_until', p.retain_until,
        'effective_retain_until', p.effective_retain_until,
        'indefinite_retention', p.indefinite_retention,
        'peer_assertions', p.peer_assertions,
        'declared_backup_holds', p.declared_backup_holds,
        'declared_migration_holds', p.declared_migration_holds,
        'size_bytes', p.size_bytes,
        'identity_state', p.current_identity,
        'status', CASE WHEN p.protected THEN 'protected'
            WHEN p.retained THEN 'retained' ELSE 'blocked' END,
        'policy_candidate', p.policy_candidate,
        'roots', jsonb_build_object(
            'asset_locations', p.asset_locations, 'dataset_items', p.dataset_items,
            'tasks', p.tasks, 'task_dataset_links', p.task_dataset_links,
            'training_versions', p.training_versions,
            'active_learning_roots', p.active_learning_roots,
            'ambiguous_legacy_tasks', p.ambiguous_legacy_tasks
        ),
        'blockers', to_jsonb(array_remove(ARRAY[
            CASE WHEN p.asset_locations > 0 THEN 'current_asset_location' END,
            CASE WHEN p.dataset_items > 0 THEN 'current_dataset_item' END,
            CASE WHEN p.tasks > 0 THEN 'current_task' END,
            CASE WHEN p.task_dataset_links > 0 THEN 'current_task_dataset_link' END,
            CASE WHEN p.training_versions > 0 THEN 'retained_training_version' END,
            CASE WHEN p.active_learning_roots > 0 THEN 'active_learning_history' END,
            CASE WHEN p.retain_until IS NULL THEN 'indefinite_retention' END,
            CASE WHEN p.retain_until > p.measured_at THEN 'retention_window' END,
            CASE WHEN p.retain_until IS NOT NULL AND p.indefinite_retention
                THEN 'related_indefinite_retention' END,
            CASE WHEN p.effective_retain_until > p.measured_at
                AND p.retain_until < p.effective_retain_until
                THEN 'related_retention_window' END,
            CASE WHEN p.declared_backup_holds > 0 THEN 'declared_backup_hold' END,
            CASE WHEN p.declared_migration_holds > 0 THEN 'declared_migration_hold' END,
            CASE WHEN p.current_identity = 'unknown' THEN 'storage_identity_unknown' END,
            CASE WHEN p.current_identity = 'mismatch' THEN 'storage_identity_mismatch' END,
            CASE WHEN p.asset_verification_state <> 'verified' OR p.sha256 IS NULL
                OR p.sha256 !~ '^[0-9a-f]64$' OR p.size_bytes IS NULL
                THEN 'content_identity_unverified' END,
            CASE WHEN p.reference_state <> 'present' OR p.verified_at IS NULL
                THEN 'locator_unverified' END,
            CASE WHEN p.unknown_training_versions > 0 THEN 'unsupported_training_snapshot' END,
            CASE WHEN p.unknown_active_learning_rounds > 0 THEN 'unsupported_active_learning_snapshot' END,
            CASE WHEN p.ambiguous_legacy_tasks > 0 THEN 'legacy_task_bucket_unknown' END,
            'exclusive_ownership_unproven', 'backup_holds_unknown',
            'migration_holds_unknown', 'object_presence_not_checked'
        ]::text[], NULL))
    ) ORDER BY p.retired_at, p.id) FROM page p), '[]'::jsonb) AS items
FROM clock CROSS JOIN totals CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
) p WHERE p_mode = 'preview'
UNION ALL
SELECT g.measured_at, NULL::jsonb, NULL::jsonb, NULL::jsonb, to_jsonb(g), NULL::jsonb
FROM groups g WHERE p_mode = 'reclaim'
UNION ALL
SELECT r.measured_at, NULL::jsonb, r.counts, NULL::jsonb, NULL::jsonb, r.unknown
FROM (
SELECT clock.measured_at,
    jsonb_build_object(
        'total_assertions', (SELECT count(*) FROM public.managed_original_retirements),
        'physical_groups', count(g.*),
        'protected_groups', count(g.*) FILTER (WHERE g.protected),
        'retained_groups', count(g.*) FILTER (WHERE NOT g.protected AND g.retained),
        'blocked_groups', count(g.*) FILTER (WHERE NOT g.protected AND NOT g.retained AND g.blocked),
        'unreferenced_groups', count(g.*) FILTER (WHERE NOT g.protected AND NOT g.retained AND NOT g.blocked)
    ) AS counts,
    jsonb_build_object(
        'unknown_training_versions', u.count,
        'unknown_active_learning_rounds', au.count,
        'unknown_reference_aliases', ua.count,
        'unknown_operations', uo.count
    ) AS unknown
FROM clock CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
CROSS JOIN unknown_aliases ua CROSS JOIN unknown_operations uo
LEFT JOIN grouped g ON true
GROUP BY clock.measured_at, u.count, au.count, ua.count, uo.count
) r WHERE p_mode = 'reconcile'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.observe_original_reclamation(
    text,uuid,text,text,text,jsonb,uuid,uuid,integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_scope(p_source uuid, p_asset uuid, p_lock boolean)
RETURNS TABLE(location_id uuid, sha256 text, size_bytes bigint, source_revision integer,
    policy_revision integer, location_revision integer, cache_quota jsonb, cache_ttl jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_location uuid;
BEGIN
    IF p_lock IS TRUE THEN
        SELECT s.location_id INTO v_location FROM public.dataset_sources s WHERE s.id = p_source;
        IF NOT FOUND THEN RETURN; END IF;
        PERFORM 1 FROM public.storage_locations l WHERE l.id = v_location FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
        PERFORM 1 FROM public.dataset_sources s WHERE s.id = p_source AND s.location_id = v_location FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY
        SELECT l.id, a.checksum_value::text, a.size_bytes, s.config_revision,
            s.policy_revision, l.config_revision, l.config->'original_cache_quota_bytes',
            coalesce(l.config->'original_cache_ttl_seconds', '86400'::jsonb)
        FROM public.dataset_sources s
        JOIN public.storage_locations l ON l.id = s.location_id
        JOIN public.dataset_items i ON i.dataset_id = s.dataset_id
        JOIN public.dataset_assets a ON a.dataset_item_id = i.id
        WHERE s.id = p_source AND a.id = p_asset AND s.enabled AND s.unregistered_at IS NULL
            AND s.ownership_mode = 'reference' AND s.cache_policy = 'on_demand' AND l.enabled
            AND l.kind IN ('filesystem','s3','sftp','agent') AND l.config->>'cache_policy' = 'on_demand'
            AND a.verification_state = 'verified' AND a.checksum_algorithm = 'sha256'
            AND a.media_type = 'image' AND a.size_bytes > 0;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_usage(p_location uuid)
RETURNS TABLE(state text, charged numeric, entries bigint, failed bigint, uncertain bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT e.state::text, sum(e.size_bytes), count(*), count(*) FILTER (WHERE e.error_code IS NOT NULL),
        count(*) FILTER (WHERE e.uncertain_write)
    FROM public.original_cache_entries e WHERE e.scope_location_id = p_location GROUP BY e.state
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_charge(p_location uuid)
RETURNS numeric LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT coalesce(sum(e.size_bytes), 0) FROM public.original_cache_entries e WHERE e.scope_location_id = p_location
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_current_state(p_asset uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT e.state FROM public.original_cache_entries e WHERE e.asset_id = p_asset AND e.state IN ('uploading','ready')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_entry_facts(p_entry uuid, p_lock text)
RETURNS SETOF public.original_cache_entries
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    CASE p_lock
        WHEN 'read' THEN RETURN QUERY SELECT e.* FROM public.original_cache_entries e WHERE e.id = p_entry;
        WHEN 'update' THEN RETURN QUERY SELECT e.* FROM public.original_cache_entries e WHERE e.id = p_entry FOR UPDATE;
        WHEN 'skip' THEN RETURN QUERY SELECT e.* FROM public.original_cache_entries e WHERE e.id = p_entry FOR UPDATE SKIP LOCKED;
        ELSE RAISE EXCEPTION 'Invalid original cache lock' USING ERRCODE = '22023';
    END CASE;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reserve_original_cache_generation(p_request jsonb)
RETURNS timestamptz LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.original_cache_entries (id, scope_location_id, source_id, asset_id, sha256,
        size_bytes, source_revision, policy_revision, location_revision, location_id, bucket, binding_generation,
        storage_identity, state, uncertain_write, last_accessed_at, expires_at, writer_until, retain_until,
        next_check_at, storage_ref)
    SELECT x.id, x.scope_location_id, x.source_id, x.asset_id, x.sha256, x.size_bytes, x.source_revision,
        x.policy_revision, x.location_revision, (x.storage_ref->>'location_id')::uuid, x.storage_ref->>'bucket',
        (x.storage_ref->>'binding_generation')::integer, x.storage_ref->>'storage_identity',
        'uploading', true, x.last_accessed_at, x.expires_at, x.writer_until, x.retain_until, x.writer_until, x.storage_ref
    FROM jsonb_to_record(p_request) x(
        id uuid, scope_location_id uuid, source_id uuid, asset_id uuid, sha256 text, size_bytes bigint,
        source_revision integer, policy_revision integer, location_revision integer, storage_ref jsonb,
        last_accessed_at timestamptz, expires_at timestamptz, writer_until timestamptz, retain_until timestamptz)
    RETURNING created_at
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._original_cache_object_key(p_entry public.original_cache_entries)
RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT coalesce((p_entry.storage_ref->>'prefix') || 'payload',
        'original-cache/v1/' || p_entry.asset_id::text || '/' || p_entry.sha256 || '/' || p_entry.id::text)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_has_readers(p_entry uuid, p_checked timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.original_cache_read_leases r
        WHERE r.entry_id = p_entry AND r.expires_at > p_checked)
    OR EXISTS (SELECT 1 FROM public.storage_operations o
        WHERE o.purpose = 'original_cache_read_' || replace(p_entry::text,'-',''))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.retire_original_cache_generation(
    p_entry uuid, p_checked timestamptz, p_next_check timestamptz, p_uncertain boolean
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_entry public.original_cache_entries;
BEGIN
    SELECT e.* INTO v_entry FROM public.original_cache_entries e WHERE e.id = p_entry FOR UPDATE;
    IF NOT FOUND OR yoao_assets_v1.original_cache_has_readers(p_entry, p_checked) THEN RETURN false; END IF;
    DELETE FROM public.dataset_asset_locations l
        WHERE l.asset_id = v_entry.asset_id AND l.location_id = v_entry.location_id AND l.bucket = v_entry.bucket
            AND l.object_key = yoao_assets_v1._original_cache_object_key(v_entry) AND l.role = 'original_cache';
    UPDATE public.original_cache_entries SET state = 'deleting', next_check_at = p_next_check,
        uncertain_write = coalesce(p_uncertain, uncertain_write) WHERE id = p_entry;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.retire_original_cache_page(
    p_location uuid, p_recorded_before timestamptz, p_checked timestamptz, p_limit integer
)
RETURNS TABLE(retired integer, leased integer, remaining bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid;
BEGIN
    retired := 0;
    leased := 0;
    FOR v_id IN SELECT e.id FROM public.original_cache_entries e
        WHERE e.scope_location_id = p_location AND e.state = 'ready' AND e.verified_at <= p_recorded_before
        ORDER BY e.last_accessed_at, e.id LIMIT LEAST(p_limit, 100) FOR UPDATE SKIP LOCKED
    LOOP
        IF yoao_assets_v1.retire_original_cache_generation(v_id,p_checked,p_checked,NULL) THEN retired := retired + 1;
        ELSE leased := leased + 1; END IF;
    END LOOP;
    SELECT count(*) INTO remaining FROM public.original_cache_entries e
        WHERE e.scope_location_id = p_location AND e.state = 'ready';
    RETURN NEXT;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_original_cache_generation(p_entry uuid, p_checked timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_entry public.original_cache_entries;
BEGIN
    SELECT e.* INTO v_entry FROM public.original_cache_entries e WHERE e.id = p_entry
        AND e.state = 'uploading' AND e.writer_until > p_checked AND e.expires_at > p_checked FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.dataset_asset_locations (asset_id, location_id, bucket, object_key, role, reference_state, verified_at)
    VALUES (v_entry.asset_id, v_entry.location_id, v_entry.bucket,
        yoao_assets_v1._original_cache_object_key(v_entry),'original_cache','present',p_checked)
    ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.original_cache_entries SET state = 'ready', uncertain_write = false, verified_at = p_checked,
        next_check_at = least(expires_at, p_checked + interval '60 seconds') WHERE id = p_entry;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_original_cache_read(p_source uuid, p_asset uuid)
RETURNS SETOF public.original_cache_entries
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT e.* FROM public.original_cache_entries e WHERE e.source_id = p_source AND e.asset_id = p_asset
        AND e.state = 'ready' FOR UPDATE
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_locator_current(p_entry uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.original_cache_entries e JOIN public.dataset_asset_locations l
        ON l.asset_id = e.asset_id AND l.location_id = e.location_id AND l.bucket = e.bucket
        WHERE e.id = p_entry AND e.state = 'ready' AND l.role = 'original_cache'
            AND l.reference_state = 'present' AND l.object_key = yoao_assets_v1._original_cache_object_key(e))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_original_cache_read(p_entry uuid,p_lease uuid,p_checked timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.original_cache_entries SET last_accessed_at = p_checked
        WHERE id = p_entry AND state = 'ready' AND expires_at > p_checked
            AND yoao_assets_v1.original_cache_locator_current(p_entry);
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.original_cache_read_leases (id, entry_id, expires_at)
        VALUES (p_lease, p_entry, p_checked + interval '60 seconds');
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_lease_current(p_entry uuid, p_lease uuid, p_checked timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.original_cache_read_leases l
        WHERE l.id = p_lease AND l.entry_id = p_entry AND l.expires_at > p_checked)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.end_original_cache_read(p_entry uuid, p_lease uuid)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    DELETE FROM public.original_cache_read_leases WHERE id = p_lease AND entry_id = p_entry
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.expire_original_cache_reads(p_entry uuid, p_checked timestamptz)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    DELETE FROM public.original_cache_read_leases WHERE entry_id = p_entry AND expires_at <= p_checked
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.evict_original_cache_generation(p_location uuid, p_checked timestamptz)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid;
BEGIN
    SELECT e.id INTO v_id FROM public.original_cache_entries e WHERE e.scope_location_id = p_location AND e.state = 'ready'
        AND NOT EXISTS (SELECT 1 FROM public.original_cache_read_leases r WHERE r.entry_id = e.id AND r.expires_at > p_checked)
    ORDER BY e.last_accessed_at, e.id LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF FOUND AND yoao_assets_v1.retire_original_cache_generation(v_id,p_checked,p_checked,NULL) THEN RETURN v_id; END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reschedule_original_cache_check(p_entry uuid, p_checked timestamptz)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.original_cache_entries SET next_check_at = least(expires_at, p_checked + interval '60 seconds')
        WHERE id = p_entry AND state = 'ready'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_original_cache_cleanup_error(p_entry uuid, p_error text)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.original_cache_entries SET error_code = p_error
    WHERE id = p_entry AND state = 'deleting' AND (p_error IS NULL OR p_error IN ('storage_identity_changed','storage_cleanup_failed'))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_original_cache_cleanup(p_entry uuid, p_checked timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_entry public.original_cache_entries;
BEGIN
    SELECT e.* INTO v_entry FROM public.original_cache_entries e WHERE e.id = p_entry AND e.state = 'deleting' FOR UPDATE;
    IF NOT FOUND THEN RETURN true; END IF;
    IF v_entry.uncertain_write AND v_entry.retain_until > p_checked THEN
        UPDATE public.original_cache_entries SET error_code = NULL WHERE id = p_entry;
        RETURN false;
    END IF;
    DELETE FROM public.original_cache_entries WHERE id = p_entry;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.due_original_cache_checks(p_checked timestamptz, p_limit integer)
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT e.id FROM public.original_cache_entries e WHERE e.next_check_at <= p_checked
        ORDER BY e.next_check_at, e.last_accessed_at, e.id LIMIT LEAST(p_limit, 500)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.original_cache_scope(uuid,uuid,boolean),
    yoao_assets_v1.original_cache_usage(uuid),
    yoao_assets_v1.original_cache_charge(uuid),
    yoao_assets_v1.original_cache_current_state(uuid),
    yoao_assets_v1.original_cache_entry_facts(uuid,text),
    yoao_assets_v1.reserve_original_cache_generation(jsonb),
    yoao_assets_v1._original_cache_object_key(public.original_cache_entries),
    yoao_assets_v1.original_cache_has_readers(uuid,timestamptz),
    yoao_assets_v1.retire_original_cache_generation(uuid,timestamptz,timestamptz,boolean),
    yoao_assets_v1.retire_original_cache_page(uuid,timestamptz,timestamptz,integer),
    yoao_assets_v1.publish_original_cache_generation(uuid,timestamptz),
    yoao_assets_v1.lock_original_cache_read(uuid,uuid),
    yoao_assets_v1.original_cache_locator_current(uuid),
    yoao_assets_v1.begin_original_cache_read(uuid,uuid,timestamptz),
    yoao_assets_v1.original_cache_lease_current(uuid,uuid,timestamptz),
    yoao_assets_v1.end_original_cache_read(uuid,uuid),
    yoao_assets_v1.expire_original_cache_reads(uuid,timestamptz),
    yoao_assets_v1.evict_original_cache_generation(uuid,timestamptz),
    yoao_assets_v1.reschedule_original_cache_check(uuid,timestamptz),
    yoao_assets_v1.record_original_cache_cleanup_error(uuid,text),
    yoao_assets_v1.finish_original_cache_cleanup(uuid,timestamptz),
    yoao_assets_v1.due_original_cache_checks(timestamptz,integer)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_attempt_facts(p_attempt uuid,p_lock boolean)
RETURNS SETOF public.managed_original_reclaim_attempts
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        RETURN QUERY SELECT a.* FROM public.managed_original_reclaim_attempts a WHERE a.id = p_attempt FOR UPDATE;
    ELSE
        RETURN QUERY SELECT a.* FROM public.managed_original_reclaim_attempts a WHERE a.id = p_attempt;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_attempt_for_job(p_job uuid)
RETURNS SETOF public.managed_original_reclaim_attempts LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT a.* FROM public.managed_original_reclaim_attempts a WHERE a.async_job_id = p_job
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_history_page(p_retirement uuid,p_limit integer,p_offset integer)
RETURNS SETOF public.managed_original_reclaim_attempts LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT a.* FROM public.managed_original_reclaim_attempts a WHERE a.retirement_id = p_retirement
        ORDER BY a.requested_at DESC,a.id DESC LIMIT LEAST(p_limit,100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_history_count(p_retirement uuid)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FROM public.managed_original_reclaim_attempts a WHERE a.retirement_id = p_retirement
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_claim_facts(p_bucket text,p_key text,p_lock boolean)
RETURNS SETOF public.managed_original_reclaim_claims
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        RETURN QUERY SELECT c.* FROM public.managed_original_reclaim_claims c WHERE c.bucket = p_bucket AND c.object_key = p_key FOR UPDATE;
    ELSE
        RETURN QUERY SELECT c.* FROM public.managed_original_reclaim_claims c WHERE c.bucket = p_bucket AND c.object_key = p_key;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_creation_active(p_creation uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.managed_original_reclaim_attempts a
        WHERE a.creation_id = p_creation AND a.status IN ('pending','running'))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.admit_original_reclaim(p_request jsonb,p_claim uuid,p_revision integer)
RETURNS SETOF public.managed_original_reclaim_attempts
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_attempt public.managed_original_reclaim_attempts;
BEGIN
    INSERT INTO public.managed_original_reclaim_attempts (
        id, retirement_id, creation_id, async_job_id, retry_of_id, requested_by, task_id, authority,
        target, input_fingerprint, version_id, expected_sha256, expected_size_bytes, maintenance_revision, reason
    )
    SELECT x.id, x.retirement_id, x.creation_id, x.async_job_id, x.retry_of_id, x.requested_by, x.task_id, x.authority,
        x.target, x.input_fingerprint, x.version_id, x.expected_sha256, x.expected_size_bytes, x.maintenance_revision, x.reason
    FROM jsonb_to_record(p_request) x(id uuid, retirement_id uuid, creation_id uuid, async_job_id uuid,
        retry_of_id uuid, requested_by uuid, task_id text, authority jsonb, target jsonb, input_fingerprint text,
        version_id text, expected_sha256 text, expected_size_bytes bigint, maintenance_revision integer, reason text)
    JOIN public.managed_original_retirements r ON r.id = x.retirement_id AND r.creation_id = x.creation_id
    JOIN public.managed_original_creations c ON c.id = x.creation_id AND c.status = 'verified'
    JOIN LATERAL yoao_jobs_v1.read_job(x.async_job_id) j ON true
    WHERE j.kind = 'managed_original_reclaim' AND j.status = 'pending' AND j.user_id = x.requested_by
        AND j.celery_task_id = x.task_id AND j.payload->>'reclaim_id' = x.id::text
        AND j.payload->>'retirement_id' = r.id::text
        AND c.version_id = x.version_id AND c.sha256 = x.expected_sha256 AND c.size_bytes = x.expected_size_bytes
        AND c.bucket = r.bucket AND c.object_key = r.object_key AND c.location_id = r.location_id
        AND c.bucket = x.target->>'bucket' AND c.object_key = x.target->>'object_key'
        AND c.location_id::text = x.target->>'location_id' AND c.storage_identity = x.target->>'storage_identity'
    RETURNING * INTO STRICT v_attempt;
    IF p_claim IS NOT NULL THEN
        UPDATE public.managed_original_reclaim_claims c SET current_attempt_id = v_attempt.id,
            state = CASE WHEN c.state = 'tombstone' THEN c.state ELSE 'claimed' END,
            revision = c.revision + 1, updated_at = now()
        WHERE c.id = p_claim AND c.revision = p_revision AND c.state <> 'deleted'
            AND c.bucket = v_attempt.target->>'bucket' AND c.object_key = v_attempt.target->>'object_key'
            AND c.storage_identity = v_attempt.target->>'storage_identity' AND c.creation_id = v_attempt.creation_id
            AND c.version_id = v_attempt.version_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Reclaim claim changed' USING ERRCODE = '23514'; END IF;
    ELSE
        INSERT INTO public.managed_original_reclaim_claims (id,bucket,object_key,storage_identity,creation_id,
            version_id,current_attempt_id,state)
        VALUES (gen_random_uuid(),v_attempt.target->>'bucket',v_attempt.target->>'object_key',
            v_attempt.target->>'storage_identity',v_attempt.creation_id,v_attempt.version_id,v_attempt.id,'claimed');
    END IF;
    RETURN NEXT v_attempt;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_original_reclaim_attempt(p_attempt uuid,p_task text,p_started timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_job uuid;
BEGIN
    UPDATE public.managed_original_reclaim_attempts a SET status = 'running', started_at = p_started,
        phase = 'reading' WHERE a.id = p_attempt AND a.task_id = p_task AND a.status = 'pending'
        AND a.cancel_requested_at IS NULL
        AND EXISTS (SELECT 1 FROM public.managed_original_reclaim_claims c WHERE c.current_attempt_id = a.id
            AND c.state IN ('claimed','tombstone'))
        RETURNING a.async_job_id INTO v_job;
    IF NOT FOUND THEN RETURN false; END IF;
    IF NOT yoao_jobs_v1.sync_original_maintenance_job(v_job,'managed_original_reclaim',p_task,
        'pending','running',p_started,NULL,NULL,false) THEN
        RAISE EXCEPTION 'Reclaim job changed' USING ERRCODE = '23514';
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.request_original_reclaim_cancel(p_attempt uuid,p_task text,p_cancel timestamptz)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.managed_original_reclaim_attempts a SET cancel_requested_at = coalesce(a.cancel_requested_at,p_cancel)
    WHERE a.id = p_attempt AND a.task_id = p_task AND a.status IN ('pending','running')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_original_reclaim_attempt(
    p_attempt uuid,p_task text,p_previous text,p_status text,p_outcome text,p_error text,p_at timestamptz,
    p_cancel timestamptz,p_settled boolean,p_ack jsonb,p_observation jsonb
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_attempt public.managed_original_reclaim_attempts;
BEGIN
    UPDATE public.managed_original_reclaim_attempts a SET status = p_status, outcome = p_outcome,
        error_code = p_error, completed_at = p_at, cancel_requested_at = coalesce(a.cancel_requested_at,p_cancel),
        physical_settled = p_settled, phase = CASE WHEN p_settled THEN 'settled' ELSE a.phase END,
        delete_ack = coalesce(a.delete_ack,p_ack), observation = p_observation
    WHERE a.id = p_attempt AND a.task_id = p_task AND a.status = p_previous
        AND a.status IN ('pending','running') AND p_status IN ('completed','failed','cancelled','unknown')
        AND EXISTS (SELECT 1 FROM public.managed_original_reclaim_claims c WHERE c.current_attempt_id = a.id)
        AND (NOT p_settled OR NOT EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id = a.operation_id))
    RETURNING a.* INTO v_attempt;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.managed_original_reclaim_claims c SET revision = c.revision + 1, updated_at = now(), state = CASE
        WHEN v_attempt.absence_verified_at IS NOT NULL AND p_settled THEN 'deleted'
        WHEN v_attempt.delete_intent_at IS NOT NULL THEN 'tombstone'
        WHEN p_settled AND c.state <> 'tombstone' THEN 'released'
        ELSE c.state END
    WHERE c.current_attempt_id = p_attempt;
    PERFORM yoao_jobs_v1.sync_original_maintenance_job(v_attempt.async_job_id,'managed_original_reclaim',p_task,
        NULL,CASE WHEN p_status = 'unknown' THEN 'failed' ELSE p_status END,p_at,p_error,
        jsonb_build_object('reclaim_id',p_attempt,'outcome',p_outcome,'error_code',p_error,
            'delete_started',v_attempt.delete_intent_at IS NOT NULL,
            'absence_proven',v_attempt.absence_verified_at IS NOT NULL,'physical_settled',p_settled),true);
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_storage_v1.begin_stopped_original_reclaim(
    p_operation uuid,p_location uuid,p_revision integer,p_node text
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM yoao_assets_storage_v1.gate_facts(p_location,'share_nowait') g
        WHERE g.state = 'stopped' AND g.revision = p_revision;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.storage_operations (id,location_id,revision,purpose,node_id)
        VALUES (p_operation,p_location,p_revision,'managed_original_reclaim',p_node);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_storage_v1.begin_stopped_original_reclaim(uuid,uuid,integer,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.acquire_original_reclaim_operation(
    p_attempt uuid,p_task text,p_operation uuid,p_node text
)
RETURNS TABLE(id uuid,location_id uuid,revision integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_attempt public.managed_original_reclaim_attempts;
        v_location uuid;
BEGIN
    SELECT a.* INTO v_attempt FROM public.managed_original_reclaim_attempts a
    JOIN public.managed_original_reclaim_claims c ON c.current_attempt_id = a.id
    JOIN LATERAL yoao_jobs_v1.read_job(a.async_job_id) j ON true
    WHERE a.id = p_attempt AND a.status = 'running' AND a.task_id = p_task
        AND a.operation_id IS NULL AND a.cancel_requested_at IS NULL AND a.physical_settled
        AND j.kind = 'managed_original_reclaim' AND j.status = 'running' AND j.celery_task_id = p_task
        AND j.user_id = a.requested_by AND j.payload->>'reclaim_id' = a.id::text
        AND j.payload->>'retirement_id' = a.retirement_id::text
        AND c.state IN ('claimed','tombstone') AND c.creation_id = a.creation_id AND c.version_id = a.version_id
        AND c.bucket = a.target->>'bucket' AND c.object_key = a.target->>'object_key'
        AND c.storage_identity = a.target->>'storage_identity'
    FOR UPDATE OF a,c;
    IF NOT FOUND THEN RETURN; END IF;
    v_location := (v_attempt.target->>'location_id')::uuid;
    IF NOT yoao_assets_storage_v1.begin_stopped_original_reclaim(
        p_operation,v_location,v_attempt.maintenance_revision,p_node
    ) THEN RETURN; END IF;
    UPDATE public.managed_original_reclaim_attempts a SET operation_id = p_operation, physical_settled = false
        WHERE a.id = p_attempt;
    RETURN QUERY SELECT p_operation,v_location,v_attempt.maintenance_revision;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reclaim_version_may_be_deleted(p_creation uuid,p_version text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.managed_original_reclaim_attempts a WHERE a.creation_id = p_creation
        AND a.version_id = p_version AND a.delete_intent_at IS NOT NULL)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.checkpoint_original_reclaim(
    p_attempt uuid,p_task text,p_phase text,p_facts jsonb,p_at timestamptz
)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_attempt public.managed_original_reclaim_attempts;
        v_claim public.managed_original_reclaim_claims;
BEGIN
    SELECT a.* INTO v_attempt FROM public.managed_original_reclaim_attempts a
        WHERE a.id = p_attempt AND a.task_id = p_task AND a.status = 'running' FOR UPDATE;
    IF NOT FOUND THEN RETURN 'reclaim_operation_invalid'; END IF;
    SELECT c.* INTO v_claim FROM public.managed_original_reclaim_claims c WHERE c.current_attempt_id = p_attempt FOR UPDATE;
    IF NOT FOUND THEN RETURN 'reclaim_claim_changed'; END IF;
    IF p_facts->>'version_id' IS DISTINCT FROM v_attempt.version_id THEN RETURN 'reclaim_version_changed'; END IF;
    IF p_phase IN ('read_verified','delete_started') AND (
        p_facts->>'sha256' IS DISTINCT FROM v_attempt.expected_sha256
        OR p_facts->'size_bytes' IS DISTINCT FROM to_jsonb(v_attempt.expected_size_bytes)
    ) THEN RETURN 'reclaim_content_changed'; END IF;
    CASE p_phase
        WHEN 'read_verified' THEN
            UPDATE public.managed_original_reclaim_attempts SET read_verified = p_facts WHERE id = p_attempt AND read_verified IS NULL;
        WHEN 'delete_started' THEN
            IF v_attempt.cancel_requested_at IS NOT NULL THEN RETURN 'reclaim_cancelled'; END IF;
            IF v_attempt.physical_settled OR NOT EXISTS (
                SELECT 1 FROM public.storage_operations o WHERE o.id = v_attempt.operation_id
                    AND o.location_id::text = v_attempt.target->>'location_id'
                    AND o.revision = v_attempt.maintenance_revision AND o.purpose = 'managed_original_reclaim'
            ) THEN RETURN 'reclaim_operation_invalid'; END IF;
            UPDATE public.managed_original_reclaim_attempts SET phase = 'delete_intent',
                delete_intent_at = coalesce(delete_intent_at,p_at) WHERE id = p_attempt;
            UPDATE public.managed_original_reclaim_claims c SET state = 'tombstone', revision = c.revision + 1,
                updated_at = now() WHERE c.id = v_claim.id AND c.state <> 'tombstone';
        WHEN 'delete_ack' THEN
            IF v_attempt.delete_intent_at IS NULL THEN RETURN 'reclaim_intent_missing'; END IF;
            IF v_attempt.delete_ack IS NOT NULL AND v_attempt.delete_ack <> p_facts THEN RETURN 'reclaim_receipt_changed'; END IF;
            UPDATE public.managed_original_reclaim_attempts SET delete_ack = coalesce(delete_ack,p_facts),
                phase = CASE WHEN absence_verified_at IS NULL THEN 'delete_ack' ELSE phase END WHERE id = p_attempt;
        WHEN 'version_absent' THEN
            IF v_attempt.delete_intent_at IS NULL AND NOT yoao_assets_v1.reclaim_version_may_be_deleted(
                v_attempt.creation_id,v_attempt.version_id) THEN RETURN 'reclaim_intent_missing'; END IF;
            UPDATE public.managed_original_reclaim_attempts SET phase = 'absence_verified',
                absence_verified_at = coalesce(absence_verified_at,p_at) WHERE id = p_attempt;
        ELSE RETURN 'reclaim_checkpoint_invalid';
    END CASE;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_reclaim_gate_blocked(p_location uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.managed_original_reclaim_claims c
        JOIN public.managed_original_reclaim_attempts a ON a.id = c.current_attempt_id
        WHERE a.target->>'location_id' = p_location::text AND c.state = 'claimed')
    OR EXISTS (SELECT 1 FROM public.managed_original_reclaim_attempts a
        WHERE a.target->>'location_id' = p_location::text AND (a.status IN ('pending','running') OR NOT a.physical_settled))
    OR EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.location_id = p_location AND o.purpose = 'managed_original_reclaim')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_reclaim_source_blocked(p_buckets text[])
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.managed_original_reclaim_claims c
        JOIN public.managed_original_reclaim_attempts a ON a.id = c.current_attempt_id
        WHERE c.bucket = ANY(p_buckets) AND (c.state = 'claimed' OR a.status IN ('pending','running')
            OR NOT a.physical_settled OR EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id = a.operation_id)))
    OR EXISTS (SELECT 1 FROM public.managed_original_reclaim_attempts a WHERE (a.target->>'bucket') = ANY(p_buckets)
        AND (NOT a.physical_settled OR EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id = a.operation_id)))
    OR EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.purpose = 'managed_original_reclaim'
        AND NOT EXISTS (SELECT 1 FROM public.managed_original_reclaim_attempts a WHERE a.operation_id = o.id))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_reconciliation_facts(p_lock boolean,p_initialize boolean)
RETURNS SETOF public.managed_original_reference_reconciliation
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_initialize IS TRUE THEN
        INSERT INTO public.managed_original_reference_reconciliation (id) VALUES (1) ON CONFLICT DO NOTHING;
    END IF;
    IF p_lock IS TRUE THEN
        RETURN QUERY SELECT r.* FROM public.managed_original_reference_reconciliation r WHERE r.id = 1 FOR UPDATE;
    ELSE
        RETURN QUERY SELECT r.* FROM public.managed_original_reference_reconciliation r WHERE r.id = 1;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.queue_original_reconciliation(p_job uuid,p_task text,p_expected uuid,p_previous text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH queued AS (UPDATE public.managed_original_reference_reconciliation SET status = 'pending', async_job_id = p_job,
        task_id = p_task, error_code = NULL WHERE id = 1 AND status = p_previous
            AND async_job_id IS NOT DISTINCT FROM p_expected
            AND EXISTS (SELECT 1 FROM yoao_jobs_v1.read_job(p_job) j WHERE j.kind = 'managed_reference_reconciliation'
                AND j.celery_task_id = p_task AND j.status = 'pending'
                AND j.payload->>'scope' = 'registered_retirement_references') RETURNING 1)
    SELECT EXISTS (SELECT 1 FROM queued)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.advance_original_reconciliation(
    p_job uuid,p_task text,p_previous text,p_status text,p_error text,p_result jsonb,p_at timestamptz,p_job_result jsonb
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.managed_original_reference_reconciliation SET status = p_status, error_code = p_error,
        counts = coalesce(p_result->'counts',counts), unknown = coalesce(p_result->'unknown',unknown),
        deltas = coalesce(p_result->'deltas',deltas), measured_at = coalesce((p_result->>'measured_at')::timestamptz,measured_at),
        last_success_at = coalesce((p_result->>'measured_at')::timestamptz,last_success_at)
    WHERE id = 1 AND async_job_id = p_job AND task_id = p_task AND status = p_previous AND (
        (p_previous = 'pending' AND p_status IN ('running','failed')) OR
        (p_previous = 'running' AND p_status IN ('completed','failed'))
    );
    IF NOT FOUND THEN RETURN false; END IF;
    IF NOT yoao_jobs_v1.sync_original_maintenance_job(p_job,'managed_reference_reconciliation',p_task,
        CASE WHEN p_status = 'running' THEN 'pending' END,p_status,p_at,p_error,p_job_result,p_job_result IS NOT NULL)
        AND p_status = 'running' THEN
        RAISE EXCEPTION 'Reference reconciliation job changed' USING ERRCODE = '23514';
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.reclaim_attempt_facts(uuid,boolean),
    yoao_assets_v1.reclaim_attempt_for_job(uuid),
    yoao_assets_v1.reclaim_history_page(uuid,integer,integer),
    yoao_assets_v1.reclaim_history_count(uuid),
    yoao_assets_v1.reclaim_claim_facts(text,text,boolean),
    yoao_assets_v1.reclaim_creation_active(uuid),
    yoao_assets_v1.admit_original_reclaim(jsonb,uuid,integer),
    yoao_assets_v1.claim_original_reclaim_attempt(uuid,text,timestamptz),
    yoao_assets_v1.request_original_reclaim_cancel(uuid,text,timestamptz),
    yoao_assets_v1.finish_original_reclaim_attempt(uuid,text,text,text,text,text,timestamptz,timestamptz,boolean,jsonb,jsonb),
    yoao_assets_v1.acquire_original_reclaim_operation(uuid,text,uuid,text),
    yoao_assets_v1.reclaim_version_may_be_deleted(uuid,text),
    yoao_assets_v1.checkpoint_original_reclaim(uuid,text,text,jsonb,timestamptz),
    yoao_assets_v1.original_reclaim_gate_blocked(uuid),
    yoao_assets_v1.original_reclaim_source_blocked(text[]),
    yoao_assets_v1.original_reconciliation_facts(boolean,boolean),
    yoao_assets_v1.queue_original_reconciliation(uuid,text,uuid,text),
    yoao_assets_v1.advance_original_reconciliation(uuid,text,text,text,text,jsonb,timestamptz,jsonb)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.observe_original_retention(
    p_retirement_id uuid,p_platform_id uuid,p_action text,p_retain_until timestamptz,
    p_hold_id uuid,p_hold_kind text,p_reference_code text,p_limit integer,p_offset integer
)
RETURNS TABLE(measured_at timestamptz, anchor jsonb, configuration jsonb, peer_fingerprint text,
    hold_fingerprint text, before_state jsonb, after_state jsonb, total_holds bigint,
    history jsonb, selected_hold jsonb, duplicate_reference boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
WITH anchor AS MATERIALIZED (
    SELECT * FROM public.managed_original_retirements WHERE id = CAST(p_retirement_id AS uuid)
), peers AS MATERIALIZED (
    SELECT r.* FROM public.managed_original_retirements r JOIN anchor a
        ON md5(r.bucket) = md5(a.bucket) AND md5(r.object_key) = md5(a.object_key)
        AND r.bucket = a.bucket AND r.object_key = a.object_key
), holds AS MATERIALIZED (
    SELECT h.* FROM public.managed_original_retention_holds h JOIN peers p ON p.id = h.retirement_id
), deadlines AS (
    SELECT bool_or(retain_until IS NULL) AS indefinite, max(retain_until) AS effective,
        bool_or(CASE WHEN id = CAST(p_retirement_id AS uuid) AND p_action = 'set_retention'
            THEN CAST(p_retain_until AS timestamptz) ELSE retain_until END IS NULL) AS after_indefinite,
        max(CASE WHEN id = CAST(p_retirement_id AS uuid) AND p_action = 'set_retention'
            THEN CAST(p_retain_until AS timestamptz) ELSE retain_until END) AS after_effective,
        count(*) AS peer_assertions FROM peers
), requirements AS (
    SELECT count(*) FILTER (WHERE released_at IS NULL AND kind = 'backup') AS backup_holds,
        count(*) FILTER (WHERE released_at IS NULL AND kind = 'migration') AS migration_holds,
        count(*) AS total_holds FROM holds
), history AS (
    SELECT * FROM holds ORDER BY (released_at IS NULL) DESC, created_at DESC, id LIMIT LEAST(p_limit,100) OFFSET p_offset
)
SELECT statement_timestamp() AS measured_at, to_jsonb(a) AS anchor,
    yoao_assets_v1.original_registry_snapshot(p_platform_id) AS configuration,
    (SELECT encode(sha256(convert_to(COALESCE(string_agg(
        encode(sha256(convert_to(to_jsonb(p)::text, 'UTF8')), 'hex'), '' ORDER BY p.id
    ), ''), 'UTF8')), 'hex') FROM peers p) AS peer_fingerprint,
    (SELECT encode(sha256(convert_to(COALESCE(string_agg(
        encode(sha256(convert_to(to_jsonb(h)::text, 'UTF8')), 'hex'), '' ORDER BY h.id
    ), ''), 'UTF8')), 'hex') FROM holds h) AS hold_fingerprint,
    jsonb_build_object('retain_until', a.retain_until,
        'effective_retain_until', CASE WHEN d.indefinite THEN NULL ELSE d.effective END,
        'indefinite_retention', d.indefinite, 'peer_assertions', d.peer_assertions,
        'backup_holds', q.backup_holds, 'migration_holds', q.migration_holds) AS before,
    jsonb_build_object('retain_until', CASE WHEN p_action = 'set_retention' THEN CAST(p_retain_until AS timestamptz) ELSE a.retain_until END,
        'effective_retain_until', CASE WHEN d.after_indefinite THEN NULL ELSE d.after_effective END,
        'indefinite_retention', d.after_indefinite, 'peer_assertions', d.peer_assertions,
        'backup_holds', q.backup_holds + CASE WHEN p_action = 'add_hold' AND p_hold_kind = 'backup' THEN 1
            WHEN p_action = 'release_hold' AND selected.kind = 'backup' AND selected.released_at IS NULL THEN -1 ELSE 0 END,
        'migration_holds', q.migration_holds + CASE WHEN p_action = 'add_hold' AND p_hold_kind = 'migration' THEN 1
            WHEN p_action = 'release_hold' AND selected.kind = 'migration' AND selected.released_at IS NULL THEN -1 ELSE 0 END) AS after,
    q.total_holds, COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY (h.released_at IS NULL) DESC, h.created_at DESC, h.id) FROM history h), '[]'::jsonb) AS history,
    to_jsonb(selected) AS selected_hold,
    EXISTS(SELECT 1 FROM public.managed_original_retention_holds h
        WHERE h.retirement_id = a.id AND h.kind = p_hold_kind AND h.reference_code = p_reference_code AND h.released_at IS NULL) AS duplicate_reference
FROM anchor a CROSS JOIN deadlines d CROSS JOIN requirements q
LEFT JOIN public.managed_original_retention_holds selected ON selected.id = CAST(p_hold_id AS uuid)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_retirement_anchor(p_retirement uuid,p_lock boolean)
RETURNS TABLE(id uuid,bucket text,object_key text,policy_revision integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        RETURN QUERY SELECT r.id,r.bucket::text,r.object_key::text,r.policy_revision
            FROM public.managed_original_retirements r WHERE r.id = p_retirement FOR UPDATE NOWAIT;
    ELSE
        RETURN QUERY SELECT r.id,r.bucket::text,r.object_key::text,r.policy_revision
            FROM public.managed_original_retirements r WHERE r.id = p_retirement;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_original_retention_hold(p_retirement uuid,p_hold uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM public.managed_original_retention_holds h
        WHERE h.id = p_hold AND h.retirement_id = p_retirement FOR UPDATE NOWAIT;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.apply_original_retention(
    p_retirement uuid,p_expected integer,p_action text,p_deadline timestamptz,p_hold uuid,p_hold_revision integer,
    p_kind text,p_reference text,p_reason text,p_actor uuid,p_at timestamptz
)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_revision integer;
BEGIN
    PERFORM 1 FROM public.managed_original_retirements r WHERE r.id = p_retirement
        AND r.policy_revision = p_expected FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    CASE p_action
        WHEN 'set_retention' THEN
            IF p_deadline IS NOT NULL AND p_deadline <= p_at THEN RETURN NULL; END IF;
        WHEN 'add_hold' THEN
            INSERT INTO public.managed_original_retention_holds(id,retirement_id,kind,reference_code,reason,created_by,created_at)
                VALUES (p_hold,p_retirement,p_kind,p_reference,p_reason,p_actor,p_at);
        WHEN 'release_hold' THEN
            UPDATE public.managed_original_retention_holds h SET revision = h.revision + 1,
                released_at = p_at, released_by = p_actor, release_reason = p_reason
            WHERE h.id = p_hold AND h.retirement_id = p_retirement AND h.revision = p_hold_revision
                AND h.released_at IS NULL;
            IF NOT FOUND THEN RETURN NULL; END IF;
        ELSE RAISE EXCEPTION 'Invalid original retention change' USING ERRCODE = '22023';
    END CASE;
    UPDATE public.managed_original_retirements r SET policy_revision = r.policy_revision + 1,
        retain_until = CASE WHEN p_action = 'set_retention' THEN p_deadline ELSE r.retain_until END
    WHERE r.id = p_retirement RETURNING r.policy_revision INTO v_revision;
    RETURN v_revision;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.observe_original_verification(p_retirement uuid)
RETURNS TABLE(measured_at timestamptz,anchor jsonb,configuration jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT statement_timestamp(), to_jsonb(r) - 'policy_revision' - 'retain_until',
        yoao_assets_v1.original_registry_snapshot(r.location_id)
    FROM public.managed_original_retirements r WHERE r.id = p_retirement
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_verification_facts(p_verification uuid,p_lock boolean)
RETURNS SETOF public.managed_original_verifications
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        RETURN QUERY SELECT v.* FROM public.managed_original_verifications v WHERE v.id = p_verification FOR UPDATE;
    ELSE
        RETURN QUERY SELECT v.* FROM public.managed_original_verifications v WHERE v.id = p_verification;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_verification_for_job(p_job uuid)
RETURNS SETOF public.managed_original_verifications LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT v.* FROM public.managed_original_verifications v WHERE v.async_job_id = p_job
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.active_original_verification(p_retirement uuid,p_lock boolean)
RETURNS SETOF public.managed_original_verifications
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS TRUE THEN
        RETURN QUERY SELECT v.* FROM public.managed_original_verifications v
            WHERE v.retirement_id = p_retirement AND v.status IN ('pending','running') FOR UPDATE;
    ELSE
        RETURN QUERY SELECT v.* FROM public.managed_original_verifications v
            WHERE v.retirement_id = p_retirement AND v.status IN ('pending','running');
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_verification_unsettled(p_retirement uuid)
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT v.operation_id FROM public.managed_original_verifications v
    JOIN public.storage_operations o ON o.id = v.operation_id WHERE v.retirement_id = p_retirement
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_verification_history(p_retirement uuid,p_limit integer,p_offset integer)
RETURNS SETOF public.managed_original_verifications LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT v.* FROM public.managed_original_verifications v WHERE v.retirement_id = p_retirement
        ORDER BY v.requested_at DESC,v.id DESC LIMIT LEAST(p_limit,100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_verification_count(p_retirement uuid)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FROM public.managed_original_verifications v WHERE v.retirement_id = p_retirement
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.admit_original_verification(p_request jsonb)
RETURNS SETOF public.managed_original_verifications
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.managed_original_verifications(id,retirement_id,async_job_id,requested_by,task_id,authority,
        anchor,configuration,input_fingerprint,expected_sha256,expected_size_bytes,maintenance_revision,requested_at,status,observation)
    SELECT x.id,x.retirement_id,x.async_job_id,x.requested_by,x.task_id,x.authority,x.anchor,x.configuration,x.input_fingerprint,
        x.expected_sha256,x.expected_size_bytes,x.maintenance_revision,x.requested_at,'pending','{}'::jsonb
    FROM jsonb_to_record(p_request) x(id uuid,retirement_id uuid,async_job_id uuid,requested_by uuid,task_id text,
        authority jsonb,anchor jsonb,configuration jsonb,input_fingerprint text,expected_sha256 text,
        expected_size_bytes bigint,maintenance_revision integer,requested_at timestamptz)
    JOIN public.managed_original_retirements r ON r.id = x.retirement_id
    JOIN LATERAL yoao_jobs_v1.read_job(x.async_job_id) j ON true
    WHERE j.kind = 'managed_original_verification' AND j.status = 'pending' AND j.user_id = x.requested_by
        AND j.celery_task_id = x.task_id AND j.payload->>'verification_id' = x.id::text
        AND j.payload->>'retirement_id' = x.retirement_id::text
        AND r.sha256 = x.expected_sha256 AND r.size_bytes = x.expected_size_bytes
    RETURNING *
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.sync_original_maintenance_job(
    p_job uuid,p_kind text,p_task text,p_previous text,p_status text,p_at timestamptz,p_error text,
    p_result jsonb,p_set_result boolean
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH applied AS (
        UPDATE public.async_jobs j SET status = p_status, updated_at = now(),
            started_at = CASE WHEN p_status = 'running' THEN p_at ELSE j.started_at END,
            completed_at = CASE WHEN p_status = 'running' THEN j.completed_at ELSE p_at END,
            error_message = CASE WHEN p_status = 'running' THEN j.error_message ELSE p_error END,
            progress_pct = CASE WHEN p_status = 'completed' THEN 100 ELSE j.progress_pct END,
            result = CASE WHEN p_set_result THEN p_result ELSE j.result END
        WHERE j.id = p_job AND j.kind = p_kind AND j.celery_task_id = p_task
            AND (p_previous IS NULL OR j.status = p_previous)
            AND j.kind IN ('managed_original_reclaim','managed_original_verification','managed_reference_reconciliation')
            AND ((p_status = 'running' AND j.status = 'pending') OR p_status IN ('completed','failed','cancelled'))
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM applied)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_original_verification(p_id uuid,p_task text,p_started timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_job uuid;
BEGIN
    UPDATE public.managed_original_verifications v SET status = 'running', started_at = p_started
        WHERE v.id = p_id AND v.task_id = p_task AND v.status = 'pending' AND v.cancel_requested_at IS NULL
        RETURNING v.async_job_id INTO v_job;
    IF NOT FOUND THEN RETURN false; END IF;
    IF NOT yoao_jobs_v1.sync_original_maintenance_job(v_job,'managed_original_verification',p_task,'pending','running',p_started,
        NULL,'{"stage":"reading","execution_permission":false}'::jsonb,true) THEN
        RAISE EXCEPTION 'Verification job changed' USING ERRCODE = '23514';
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.request_original_verification_cancel(p_id uuid,p_task text,p_cancel timestamptz)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.managed_original_verifications v SET cancel_requested_at = coalesce(v.cancel_requested_at,p_cancel)
    WHERE v.id = p_id AND v.task_id = p_task AND v.status IN ('pending','running')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.bind_original_verification_permit(p_id uuid,p_task text,p_operation uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH bound AS (
        UPDATE public.managed_original_verifications v SET operation_id = p_operation
        WHERE v.id = p_id AND v.task_id = p_task AND v.status = 'running'
            AND v.cancel_requested_at IS NULL AND v.operation_id IS NULL
            AND EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id = p_operation
                AND o.location_id::text = v.anchor->>'location_id' AND o.revision = v.maintenance_revision
                AND o.purpose = 'managed_original_verification') RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM bound)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_original_verification(
    p_id uuid,p_task text,p_previous text,p_status text,p_outcome text,p_error text,p_at timestamptz,
    p_cancel timestamptz,p_result jsonb
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_record public.managed_original_verifications;
BEGIN
    UPDATE public.managed_original_verifications v
    SET status = p_status, outcome = p_outcome, error_code = p_error, completed_at = p_at,
        cancel_requested_at = coalesce(v.cancel_requested_at,p_cancel),
        observed_sha256 = CASE WHEN p_result IS NULL THEN v.observed_sha256 ELSE p_result->>'sha256' END,
        observed_size_bytes = CASE WHEN p_result IS NULL THEN v.observed_size_bytes ELSE (p_result->>'size_bytes')::bigint END,
        observation = coalesce(p_result->'observation',v.observation)
    WHERE v.id = p_id AND v.task_id = p_task AND v.status = p_previous AND v.status IN ('pending','running')
        AND p_status IN ('completed','failed','cancelled') RETURNING v.* INTO v_record;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM yoao_jobs_v1.sync_original_maintenance_job(v_record.async_job_id,'managed_original_verification',p_task,
        NULL,p_status,p_at,p_error,jsonb_build_object('verification_id',p_id,'outcome',p_outcome,'error_code',p_error,
        'execution_permission',false,'cancel_requested',v_record.cancel_requested_at IS NOT NULL),true);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.observe_original_retention(uuid,uuid,text,timestamptz,uuid,text,text,integer,integer),
    yoao_assets_v1.original_retirement_anchor(uuid,boolean),
    yoao_assets_v1.lock_original_retention_hold(uuid,uuid),
    yoao_assets_v1.apply_original_retention(uuid,integer,text,timestamptz,uuid,integer,text,text,text,uuid,timestamptz),
    yoao_assets_v1.observe_original_verification(uuid),
    yoao_assets_v1.original_verification_facts(uuid,boolean),
    yoao_assets_v1.original_verification_for_job(uuid),
    yoao_assets_v1.active_original_verification(uuid,boolean),
    yoao_assets_v1.original_verification_unsettled(uuid),
    yoao_assets_v1.original_verification_history(uuid,integer,integer),
    yoao_assets_v1.original_verification_count(uuid),
    yoao_assets_v1.admit_original_verification(jsonb),
    yoao_jobs_v1.sync_original_maintenance_job(uuid,text,text,text,text,timestamptz,text,jsonb,boolean),
    yoao_assets_v1.claim_original_verification(uuid,text,timestamptz),
    yoao_assets_v1.request_original_verification_cancel(uuid,text,timestamptz),
    yoao_assets_v1.bind_original_verification_permit(uuid,text,uuid),
    yoao_assets_v1.finish_original_verification(uuid,text,text,text,text,text,timestamptz,timestamptz,jsonb)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_retirement_candidates(p_dataset uuid,p_item uuid)
RETURNS TABLE(dataset_id uuid,dataset_lifecycle_revision integer,dataset_item_id uuid,asset_id uuid,
    asset_verification_state text,checksum_algorithm text,checksum_value text,size_bytes bigint,
    location_id uuid,bucket text,object_key text,storage_ref jsonb,creation_id uuid,role text,
    reference_state text,etag text,verified_at timestamptz,location_kind text,location_config jsonb,
    location_config_revision integer,binding_generation integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.id,d.lifecycle_revision,i.id,a.id,a.verification_state::text,a.checksum_algorithm::text,
        a.checksum_value::text,a.size_bytes,l.location_id,l.bucket::text,l.object_key::text,l.storage_ref,
        l.creation_id,l.role::text,l.reference_state::text,l.etag,l.verified_at,s.kind::text,s.config,
        s.config_revision,b.generation
    FROM public.datasets d JOIN public.dataset_items i ON i.dataset_id = d.id
    JOIN public.dataset_assets a ON a.dataset_item_id = i.id
    JOIN public.dataset_asset_locations l ON l.asset_id = a.id
    LEFT JOIN public.storage_locations s ON s.id = l.location_id
    LEFT JOIN public.storage_bindings b ON b.purpose = 'datasets' AND b.location_id = l.location_id AND b.bucket = l.bucket
    WHERE d.id = p_dataset AND (p_item IS NULL OR i.id = p_item) AND l.role = 'managed_original'
        AND left(i.file_path, 18) <> '__aap_reference__/'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.append_original_retirement_assertions(p_dataset uuid,p_assertions jsonb)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.managed_original_retirements(id,assertion_digest,dataset_id,dataset_item_id,
        asset_id,creation_id,location_id,bucket,object_key,role,location_config_revision,binding_generation,
        storage_identity,identity_state,identity_error_code,asset_verification_state,sha256,size_bytes,
        reference_state,etag,verified_at,dataset_lifecycle_revision)
    SELECT x.id,x.assertion_digest,i.dataset_id,i.id,l.asset_id,l.creation_id,l.location_id,l.bucket,l.object_key,
        l.role,x.location_config_revision,x.binding_generation,x.storage_identity,x.identity_state,x.identity_error_code,
        a.verification_state,CASE WHEN a.verification_state = 'verified' AND a.checksum_algorithm = 'sha256'
            THEN a.checksum_value END,a.size_bytes,l.reference_state,l.etag,l.verified_at,d.lifecycle_revision
    FROM jsonb_to_recordset(p_assertions) x(id uuid,assertion_digest text,dataset_item_id uuid,
        asset_id uuid,creation_id uuid,location_id uuid,bucket text,object_key text,
        location_config_revision integer,binding_generation integer,storage_identity text,identity_state text,
        identity_error_code text)
    JOIN public.dataset_asset_locations l ON l.asset_id = x.asset_id AND l.location_id = x.location_id
        AND l.bucket = x.bucket AND l.object_key = x.object_key AND l.creation_id IS NOT DISTINCT FROM x.creation_id
        AND l.role = 'managed_original'
    JOIN public.dataset_assets a ON a.id = l.asset_id AND a.dataset_item_id = x.dataset_item_id
    JOIN public.dataset_items i ON i.id = a.dataset_item_id AND i.dataset_id = p_dataset
    JOIN public.datasets d ON d.id = p_dataset
    WHERE left(i.file_path,18) <> '__aap_reference__/'
    ON CONFLICT (assertion_digest) DO NOTHING
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.original_retirement_candidates(uuid,uuid),
    yoao_assets_v1.append_original_retirement_assertions(uuid,jsonb)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_original_binding_history()
RETURNS TABLE(asset_locations bigint, creation_allocations bigint, auxiliary_originals bigint, retirements bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT (SELECT count(*) FROM public.dataset_asset_locations),
        (SELECT count(*) FROM public.managed_original_creations WHERE owner_kind IN ('dataset','dataset_auxiliary')),
        (SELECT count(*) FROM public.dataset_auxiliary_originals),
        (SELECT count(*) FROM public.managed_original_retirements)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.original_cache_binding_history()
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FROM public.original_cache_entries
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_original_binding_history(),
    yoao_assets_v1.original_cache_binding_history() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_point_cloud_original_scenes(p_scenes uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM s.id FROM public.scenes s WHERE s.id = ANY(p_scenes) ORDER BY s.id FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_point_cloud_original_poses(p_scenes uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM p.id FROM public.scene_frame_poses p WHERE p.scene_id = ANY(p_scenes) ORDER BY p.id FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_point_cloud_original_items(p_items uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM d.id FROM public.datasets d WHERE d.id IN (
        SELECT i.dataset_id FROM public.dataset_items i WHERE i.id = ANY(p_items)
    ) ORDER BY d.id FOR SHARE NOWAIT;
    PERFORM i.id FROM public.dataset_items i WHERE i.id = ANY(p_items) ORDER BY i.id FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.lock_point_cloud_original_run(p_run uuid)
RETURNS TABLE(id uuid,project_id uuid,async_job_id uuid,requested_by_id uuid,status text,source_snapshot_digest text,
    config_digest text,scope_json jsonb,source_snapshot jsonb,config_snapshot jsonb)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id,r.project_id,r.async_job_id,r.requested_by_id,r.status::text,r.source_snapshot_digest::text,
        r.config_digest::text,r.scope_json,r.source_snapshot,r.config_snapshot
    FROM public.point_cloud_quality_runs r WHERE r.id = p_run FOR UPDATE
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.lock_point_cloud_original_configuration(p_project uuid)
RETURNS TABLE(id uuid,point_cloud_quality_config jsonb)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp SET lock_timeout = '1s'
AS $function$
    SELECT p.id,p.point_cloud_quality_config FROM public.projects p WHERE p.id = p_project FOR SHARE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.lock_point_cloud_original_scenes(uuid[]),
    yoao_assets_v1.lock_point_cloud_original_poses(uuid[]),
    yoao_assets_v1.lock_point_cloud_original_items(uuid[]),
    yoao_labeling_v1.lock_point_cloud_original_run(uuid),
    yoao_workspace_v1.lock_point_cloud_original_configuration(uuid)
FROM PUBLIC;
