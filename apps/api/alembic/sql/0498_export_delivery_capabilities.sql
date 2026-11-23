ALTER FUNCTION public.check_export_media_pair() SECURITY DEFINER;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.check_export_media_pair() FROM PUBLIC;
-- aap-statement-break
CREATE VIEW yoao_export_v1._zip_receipt AS
    SELECT id, project_id, job_id, media_bundle_id, media, format, file_count,
           sha256, size_bytes, storage_ref, upload_proof, state, created_at, expires_at
    FROM public.export_outputs;
-- aap-statement-break
CREATE VIEW yoao_export_v1._media_fence AS
    SELECT id, project_id, job_id, zip_output_id, fixture_run_id, input_digest,
           input_snapshot->'schema' AS input_schema, object_count, size_bytes,
           storage_ref, state, lease_token, lease_expires_at, created_at, expires_at,
           published_at, retired_at
    FROM public.export_media_bundles;
-- aap-statement-break
CREATE VIEW yoao_export_v1._media_delivery AS
    SELECT id, project_id, job_id, zip_output_id, fixture_run_id, input_digest,
           input_snapshot->'schema' AS input_schema, object_count, size_bytes,
           storage_ref, state, lease_token, lease_expires_at, created_at, expires_at,
           published_at, retired_at, input_snapshot, logical_paths, inventory
    FROM public.export_media_bundles;
-- aap-statement-break
CREATE VIEW yoao_export_v1._cache_entry AS
    SELECT id, output_id, cache_key, project_id, batch_id, format, object_key,
           file_count, size_bytes, expires_at
    FROM public.export_artifacts;
-- aap-statement-break
REVOKE ALL ON yoao_export_v1._zip_receipt, yoao_export_v1._media_fence,
    yoao_export_v1._media_delivery, yoao_export_v1._cache_entry FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.zip_receipt(p_id uuid, p_mode text)
RETURNS SETOF yoao_export_v1._zip_receipt LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    CASE p_mode
      WHEN 'read' THEN
        RETURN QUERY SELECT z.* FROM yoao_export_v1._zip_receipt z WHERE z.id = p_id;
      WHEN 'update' THEN
        RETURN QUERY SELECT z.* FROM yoao_export_v1._zip_receipt z WHERE z.id = p_id FOR UPDATE;
      WHEN 'share_nowait' THEN
        RETURN QUERY SELECT z.* FROM yoao_export_v1._zip_receipt z WHERE z.id = p_id FOR SHARE NOWAIT;
      WHEN 'retirement' THEN
        RETURN QUERY SELECT z.* FROM yoao_export_v1._zip_receipt z
          WHERE z.id = p_id AND (z.media_bundle_id IS NOT NULL OR z.expires_at <= clock_timestamp())
          FOR UPDATE SKIP LOCKED;
      ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid export ZIP fence';
    END CASE;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._matches_export_allocation(p_ref jsonb)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_bindings binding
        JOIN public.storage_locations location ON location.id = binding.location_id
        WHERE binding.purpose = 'export' AND location.kind = 's3' AND location.enabled
          AND binding.location_id::text = p_ref->>'location_id'
          AND binding.bucket = p_ref->>'bucket' AND binding.generation::text = p_ref->>'binding_generation'
          AND location.config_revision::text = p_ref->>'config_revision'
          AND p_ref->>'schema' = CASE WHEN binding.generation = 1 THEN '1' ELSE '2' END
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.reserve_zip(
    p_id uuid, p_project uuid, p_job uuid, p_bundle uuid, p_media text, p_format text,
    p_count integer, p_sha text, p_size bigint, p_ref jsonb,
    p_created timestamptz, p_expires timestamptz
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_bundle IS NULL AND NOT yoao_export_v1._matches_export_allocation(p_ref) THEN
        RETURN false;
    END IF;
    IF p_bundle IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.export_media_bundles b
        WHERE b.id = p_bundle AND b.zip_output_id = p_id AND b.project_id = p_project
          AND b.job_id = p_job AND b.created_at = p_created AND b.expires_at = p_expires
          AND b.state = 'building' AND b.lease_token IS NOT NULL
          AND b.lease_expires_at > clock_timestamp()
          AND p_media = CASE WHEN b.input_snapshot->'schema' = '4'::jsonb THEN 'video' ELSE 'image' END
          AND p_ref = (b.storage_ref - 'key_prefix') || jsonb_build_object(
              'object_key', left(b.storage_ref->>'key_prefix', -length('lidar-export-media/objects/' || b.id || '/'))
                  || p_media || '/' || p_project || '/' || p_job || '/' || p_id || '.zip'
          )
        FOR UPDATE NOWAIT
    ) THEN
        RETURN false;
    END IF;
    INSERT INTO public.export_outputs (
        id, project_id, job_id, media_bundle_id, media, format, file_count,
        sha256, size_bytes, storage_ref, state, created_at, expires_at
    ) VALUES (
        p_id, p_project, p_job, p_bundle, p_media, p_format, p_count,
        p_sha, p_size, p_ref, 'pending', p_created, p_expires
    );
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.save_zip_proof(p_id uuid, p_proof jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE public.export_outputs z SET upload_proof = p_proof
    WHERE z.id = p_id AND z.state = 'pending' AND z.upload_proof IS NULL
      AND p_proof IS NOT NULL
      AND z.storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.publish_zip(p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE public.export_outputs z SET state = 'ready'
    WHERE z.id = p_id AND z.state = 'pending' AND z.expires_at > clock_timestamp()
      AND (z.storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
           OR z.upload_proof IS NOT NULL);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.allocate_media(
    p_id uuid, p_project uuid, p_job uuid, p_zip uuid, p_input jsonb, p_digest text,
    p_paths jsonb, p_inventory jsonb, p_count integer, p_size bigint, p_ref jsonb,
    p_created timestamptz, p_expires timestamptz
) RETURNS TABLE(fixture_run_id uuid, created_at timestamptz, expires_at timestamptz)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    INSERT INTO public.export_media_bundles AS b (
        id, project_id, job_id, zip_output_id, input_snapshot, input_digest,
        logical_paths, inventory, object_count, size_bytes, storage_ref,
        state, created_at, expires_at
    ) SELECT p_id, p_project, p_job, p_zip, p_input, p_digest, p_paths, p_inventory,
        p_count, p_size, p_ref, 'building', p_created, p_expires
    WHERE yoao_export_v1._matches_export_allocation(p_ref)
    RETURNING b.fixture_run_id, b.created_at, b.expires_at;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_delivery(p_id uuid, p_lock boolean)
RETURNS SETOF yoao_export_v1._media_delivery LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT b.* FROM yoao_export_v1._media_delivery b WHERE b.id = p_id FOR UPDATE NOWAIT;
    ELSE
        RETURN QUERY SELECT b.* FROM yoao_export_v1._media_delivery b WHERE b.id = p_id;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_fence(p_id uuid, p_mode text)
RETURNS SETOF yoao_export_v1._media_fence LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    CASE p_mode
      WHEN 'read' THEN
        RETURN QUERY SELECT b.* FROM yoao_export_v1._media_fence b WHERE b.id = p_id;
      WHEN 'update_nowait' THEN
        RETURN QUERY SELECT b.* FROM yoao_export_v1._media_fence b WHERE b.id = p_id FOR UPDATE NOWAIT;
      WHEN 'share_nowait' THEN
        RETURN QUERY SELECT b.* FROM yoao_export_v1._media_fence b WHERE b.id = p_id FOR SHARE NOWAIT;
      ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid export media fence';
    END CASE;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.claim_media(p_id uuid, p_token uuid, p_expires timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE public.export_media_bundles b SET lease_token = p_token, lease_expires_at = p_expires
    WHERE b.id = p_id AND b.state = 'building' AND b.lease_token IS NULL AND p_token IS NOT NULL
      AND b.expires_at > clock_timestamp();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.publish_media(
    p_id uuid, p_token uuid, p_lease_expires timestamptz, p_published timestamptz
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE public.export_media_bundles b SET state = 'ready', published_at = p_published
    WHERE b.id = p_id AND b.state = 'building' AND b.lease_token = p_token
      AND b.lease_expires_at = p_lease_expires AND b.lease_expires_at > clock_timestamp();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.save_media_version(
    p_bundle uuid, p_member uuid, p_token uuid, p_lease_expires timestamptz, p_version text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    INSERT INTO public.export_media_member_versions (bundle_id, member_id, version_id)
    SELECT b.id, p_member, p_version FROM public.export_media_bundles b
    WHERE b.id = p_bundle AND b.state = 'building' AND b.lease_token = p_token
      AND b.lease_expires_at = p_lease_expires AND b.lease_expires_at > clock_timestamp()
      AND b.storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
      AND b.inventory->'objects' ? ((b.storage_ref->>'key_prefix') || p_member::text || '.bin')
    FOR UPDATE OF b;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_version(p_bundle uuid, p_member uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.version_id::text FROM public.export_media_member_versions v
    WHERE v.bundle_id = p_bundle AND v.member_id = p_member;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_versions(p_bundle uuid)
RETURNS TABLE(member_id uuid, version_id text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.member_id, v.version_id::text FROM public.export_media_member_versions v
    WHERE v.bundle_id = p_bundle;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.find_cache(p_key text, p_now timestamptz)
RETURNS SETOF yoao_export_v1._cache_entry LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT a.* FROM yoao_export_v1._cache_entry a WHERE a.cache_key = p_key AND a.expires_at > p_now;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.lock_cache(p_id uuid)
RETURNS SETOF yoao_export_v1._cache_entry LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT a.* FROM yoao_export_v1._cache_entry a WHERE a.id = p_id FOR UPDATE;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.invalidate_cache(p_id uuid, p_output uuid)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    DELETE FROM public.export_artifacts a WHERE a.id = p_id AND a.output_id = p_output;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.record_cache(
    p_key text, p_output uuid, p_batch uuid
) RETURNS SETOF yoao_export_v1._cache_entry LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    z yoao_export_v1._zip_receipt%ROWTYPE;
    a yoao_export_v1._cache_entry%ROWTYPE;
BEGIN
    SELECT candidate.* INTO z FROM yoao_export_v1._zip_receipt candidate
    WHERE candidate.id = p_output AND candidate.state = 'ready'
      AND candidate.expires_at > clock_timestamp();
    IF NOT FOUND THEN RETURN; END IF;
    INSERT INTO public.export_artifacts (
        id, output_id, cache_key, project_id, batch_id, format, object_key,
        file_count, size_bytes, expires_at
    ) VALUES (
        gen_random_uuid(), z.id, p_key, z.project_id, p_batch, z.format,
        z.storage_ref->>'object_key', z.file_count, z.size_bytes, z.expires_at
    ) ON CONFLICT (cache_key) DO NOTHING;
    SELECT entry.* INTO a FROM yoao_export_v1._cache_entry entry
    WHERE entry.cache_key = p_key FOR UPDATE;
    IF NOT FOUND OR a.project_id <> z.project_id THEN RETURN; END IF;
    IF a.output_id IS NOT NULL AND a.expires_at > clock_timestamp() THEN
        IF EXISTS (
            SELECT 1 FROM public.export_outputs old
            WHERE old.id = a.output_id AND old.state = 'ready'
              AND old.project_id = a.project_id AND old.storage_ref->>'object_key' = a.object_key
              AND old.format = a.format AND old.file_count = a.file_count
              AND old.size_bytes = a.size_bytes AND old.expires_at = a.expires_at
        ) THEN
            RETURN NEXT a;
        END IF;
        RETURN;
    END IF;
    RETURN QUERY UPDATE public.export_artifacts entry SET
        output_id = z.id, batch_id = p_batch, format = z.format,
        object_key = z.storage_ref->>'object_key', file_count = z.file_count,
        size_bytes = z.size_bytes, expires_at = z.expires_at
    WHERE entry.id = a.id
    RETURNING entry.id, entry.output_id, entry.cache_key, entry.project_id,
        entry.batch_id, entry.format, entry.object_key, entry.file_count,
        entry.size_bytes, entry.expires_at;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._media_has_operations(p_bundle uuid, p_zip uuid, p_ignore uuid)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_operations op
        WHERE op.id IS DISTINCT FROM p_ignore AND op.purpose IN (
            'export_media_' || replace(p_bundle::text, '-', ''),
            'export_write_' || replace(p_zip::text, '-', ''),
            'export_read_' || replace(p_zip::text, '-', '')
        )
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._media_attempt_ended(
    p_bundle uuid, p_project uuid, p_job uuid, p_expires timestamptz,
    p_lease_expires timestamptz, p_published timestamptz, p_now timestamptz
) RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE
        WHEN p_expires <= p_now THEN true
        WHEN p_published IS NOT NULL THEN false
        WHEN p_lease_expires <= p_now THEN true
        ELSE NOT EXISTS (
            SELECT 1 FROM public.async_jobs j
            WHERE j.id = p_job AND j.status NOT IN ('completed', 'failed', 'cancelled')
        ) OR EXISTS (
            SELECT 1 FROM public.async_jobs j
            JOIN public.export_media_bundles next ON next.job_id = j.id
            CROSS JOIN (VALUES ('lidar_media', 1), ('lidar_media', 2), ('image_media', 3), ('video_media', 4)) p(key, schema)
            WHERE j.id = p_job AND j.kind = 'export' AND j.project_id = p_project
              AND next.id <> p_bundle AND next.project_id = p_project
              AND next.input_snapshot->>'schema' = p.schema::text
              AND next.input_snapshot->'schema' = to_jsonb(p.schema)
              AND j.payload->p.key->>'schema' = p.schema::text
              AND NOT (j.payload ?| array_remove(ARRAY['lidar_media', 'image_media', 'video_media'], p.key))
              AND j.payload->p.key = jsonb_build_object(
                  'schema', p.schema, 'bundle_id', next.id::text, 'input_digest', next.input_digest
              )
        )
    END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_attempt_ended(p_id uuid, p_now timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_export_v1._media_attempt_ended(
        b.id, b.project_id, b.job_id, b.expires_at, b.lease_expires_at, b.published_at, p_now
    ) FROM public.export_media_bundles b WHERE b.id = p_id;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_has_operations(p_id uuid, p_ignore uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, p_ignore)
    FROM public.export_media_bundles b WHERE b.id = p_id;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.lock_media_retirement(p_id uuid, p_inventory boolean)
RETURNS TABLE (
    id uuid, project_id uuid, job_id uuid, zip_output_id uuid, fixture_run_id uuid,
    input_digest text, input_schema jsonb, object_count integer, size_bytes bigint,
    storage_ref jsonb, state text, lease_token uuid, lease_expires_at timestamptz,
    created_at timestamptz, expires_at timestamptz, published_at timestamptz, retired_at timestamptz,
    inventory jsonb
) LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT b.id, b.project_id, b.job_id, b.zip_output_id, b.fixture_run_id,
           b.input_digest::text, b.input_snapshot->'schema', b.object_count, b.size_bytes,
           b.storage_ref, b.state::text, b.lease_token, b.lease_expires_at,
           b.created_at, b.expires_at, b.published_at, b.retired_at,
           CASE WHEN p_inventory THEN b.inventory END
    FROM public.export_media_bundles b
    WHERE b.id = p_id AND (b.published_at IS NULL OR b.expires_at <= clock_timestamp())
    FOR UPDATE SKIP LOCKED;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.start_media_retirement(p_id uuid, p_at timestamptz)
RETURNS timestamptz LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    b record;
BEGIN
    p_at := least(p_at, clock_timestamp());
    UPDATE public.export_media_bundles owner SET last_attempted_at = p_at
    WHERE owner.id = p_id AND owner.state <> 'deleted'
      AND (owner.fixture_run_id IS NULL OR public.fixture_export_owner_session())
    RETURNING owner.id, owner.zip_output_id, owner.project_id, owner.job_id,
        owner.expires_at, owner.lease_expires_at, owner.published_at, owner.state,
        owner.retired_at INTO b;
    IF NOT FOUND OR yoao_export_v1._media_has_operations(b.id, b.zip_output_id, NULL)
       OR NOT yoao_export_v1._media_attempt_ended(
           b.id, b.project_id, b.job_id, b.expires_at, b.lease_expires_at, b.published_at, p_at
       ) THEN RETURN NULL; END IF;
    IF b.state = 'retiring' THEN RETURN b.retired_at; END IF;
    UPDATE public.export_media_bundles owner SET state = 'retiring', retired_at = p_at
    WHERE owner.id = p_id;
    RETURN p_at;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.finish_media_retirement(p_id uuid, p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE public.export_media_bundles b SET state = 'deleted'
    WHERE b.id = p_id AND b.state = 'retiring'
      AND (b.published_at IS NULL OR b.expires_at <= least(p_at, clock_timestamp()))
      AND NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, NULL);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_retirement_queue(p_now timestamptz, p_limit integer)
RETURNS SETOF uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT b.id FROM public.export_media_bundles b
    WHERE b.state <> 'deleted' AND b.fixture_run_id IS NULL
      AND NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, NULL)
      AND yoao_export_v1._media_attempt_ended(
          b.id, b.project_id, b.job_id, b.expires_at, b.lease_expires_at, b.published_at, p_now
      )
    ORDER BY b.last_attempted_at NULLS FIRST, b.created_at, b.id
    LIMIT greatest(1, least(p_limit, 100)) FOR UPDATE SKIP LOCKED;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.zip_retirement_bundle(p_id uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT z.media_bundle_id FROM public.export_outputs z WHERE z.id = p_id;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._zip_has_read(p_id uuid)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (SELECT 1 FROM public.storage_operations op
                   WHERE op.purpose = 'export_read_' || replace(p_id::text, '-', ''));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._zip_retirement_allowed(
    p_id uuid, p_bundle uuid, p_now timestamptz, p_expired boolean
) RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN p_bundle IS NULL THEN p_expired
    ELSE EXISTS (
        SELECT 1 FROM public.export_media_bundles b
        WHERE b.id = p_bundle AND b.zip_output_id = p_id
          AND (p_expired OR b.published_at IS NULL)
          AND NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, NULL)
          AND yoao_export_v1._media_attempt_ended(
              b.id, b.project_id, b.job_id, b.expires_at, b.lease_expires_at, b.published_at, p_now
          )
    ) END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.start_zip_retirement(p_id uuid, p_at timestamptz)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    z record;
    dropped bigint := 0;
    native boolean;
BEGIN
    p_at := least(p_at, clock_timestamp());
    SELECT owner.state, owner.media_bundle_id, owner.job_id, owner.project_id, owner.upload_proof INTO z
    FROM public.export_outputs owner
    WHERE owner.id = p_id AND yoao_export_v1._zip_retirement_allowed(
        owner.id, owner.media_bundle_id, p_at, owner.expires_at <= p_at
    ) AND (owner.state IN ('pending', 'retired') OR owner.expires_at <= p_at)
      AND NOT EXISTS (
          SELECT 1 FROM public.export_media_bundles b
          WHERE b.id = owner.media_bundle_id AND b.fixture_run_id IS NOT NULL
            AND (owner.upload_proof IS NULL OR NOT public.fixture_export_owner_session())
      )
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    native := z.media_bundle_id IS NULL AND EXISTS (
        SELECT 1 FROM public.fixture_native_export_lifetime(z.job_id, z.project_id)
    );
    IF native AND (NOT public.fixture_export_owner_session() OR z.upload_proof IS NULL
                   OR NOT public.fixture_native_export_cleanup_eligible(p_id)) THEN RETURN NULL; END IF;
    IF z.state = 'pending' AND NOT native AND NOT EXISTS (
        SELECT 1 FROM public.export_media_bundles b WHERE b.id = z.media_bundle_id AND b.published_at IS NULL
    ) THEN RETURN NULL; END IF;
    UPDATE public.export_outputs SET last_attempted_at = p_at WHERE id = p_id;
    IF yoao_export_v1._zip_has_read(p_id) THEN RETURN NULL; END IF;
    IF z.state = 'ready' THEN
        DELETE FROM public.export_artifacts a WHERE a.output_id = p_id;
        GET DIAGNOSTICS dropped = ROW_COUNT;
    ELSIF EXISTS (SELECT 1 FROM public.export_artifacts a WHERE a.output_id = p_id) THEN
        RETURN NULL;
    END IF;
    IF z.state <> 'retired' THEN
        UPDATE public.export_outputs SET state = 'retired', retired_at = p_at WHERE id = p_id;
    END IF;
    RETURN dropped;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.zip_retirement_unreferenced(p_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT yoao_export_v1._zip_has_read(p_id)
       AND NOT EXISTS (SELECT 1 FROM public.export_artifacts a WHERE a.output_id = p_id);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.remove_retired_zip(p_id uuid, p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    p_at := least(p_at, clock_timestamp());
    DELETE FROM public.export_outputs z
    WHERE z.id = p_id AND z.state = 'retired'
      AND yoao_export_v1.zip_retirement_unreferenced(z.id)
      AND yoao_export_v1._zip_retirement_allowed(z.id, z.media_bundle_id, p_at, z.expires_at <= p_at);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.purge_legacy_cache(p_now timestamptz, p_limit integer)
RETURNS bigint LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH expired AS (
        DELETE FROM public.export_artifacts a
        WHERE a.output_id IS NULL AND a.id IN (
            SELECT entry.id FROM public.export_artifacts entry
            WHERE entry.output_id IS NULL AND entry.expires_at <= least(p_now, clock_timestamp())
            ORDER BY entry.expires_at, entry.id LIMIT greatest(1, least(p_limit, 100))
        ) RETURNING 1
    ) SELECT count(*) FROM expired;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.zip_retirement_queue(p_now timestamptz, p_limit integer)
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT z.id FROM public.export_outputs z
    LEFT JOIN public.export_media_bundles b ON b.id = z.media_bundle_id
    WHERE (z.state = 'ready' AND z.expires_at <= p_now OR z.state = 'retired'
           OR z.state = 'pending' AND b.id IS NOT NULL AND b.published_at IS NULL
               AND yoao_export_v1._media_attempt_ended(
                   b.id, b.project_id, b.job_id, b.expires_at, b.lease_expires_at, b.published_at, p_now
               ))
      AND (b.id IS NULL OR NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, NULL))
      AND b.fixture_run_id IS NULL
      AND NOT EXISTS (SELECT 1 FROM public.fixture_native_export_lifetime(z.job_id, z.project_id))
    ORDER BY z.last_attempted_at NULLS FIRST, z.created_at, z.id
    LIMIT greatest(1, least(p_limit, 100));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.binding_history() RETURNS TABLE (
    zip_pending bigint, zip_ready bigint, zip_retired bigint,
    media_building bigint, media_ready bigint, media_retiring bigint, media_deleted bigint
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT z.*, b.* FROM (
        SELECT count(*) FILTER (WHERE state = 'pending'), count(*) FILTER (WHERE state = 'ready'),
               count(*) FILTER (WHERE state = 'retired') FROM public.export_outputs
    ) z CROSS JOIN (
        SELECT count(*) FILTER (WHERE state = 'building'), count(*) FILTER (WHERE state = 'ready'),
               count(*) FILTER (WHERE state = 'retiring'), count(*) FILTER (WHERE state = 'deleted')
        FROM public.export_media_bundles
    ) b;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.saved_location_is_referenced(p_location uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (SELECT 1 FROM public.export_outputs z WHERE z.storage_ref->>'location_id' = p_location::text)
        OR EXISTS (SELECT 1 FROM public.export_media_bundles b WHERE b.storage_ref->>'location_id' = p_location::text);
$function$;
-- aap-statement-break
CREATE VIEW yoao_export_v1.export_zip_maintenance AS
    SELECT z.id, z.project_id, z.job_id, z.media_bundle_id, z.state::text, z.storage_ref
    FROM public.export_outputs z CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
CREATE VIEW yoao_export_v1.export_media_maintenance AS
    SELECT b.id, b.project_id, b.job_id, b.zip_output_id, b.state::text, b.storage_ref
    FROM public.export_media_bundles b CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
REVOKE ALL ON yoao_export_v1.export_zip_maintenance, yoao_export_v1.export_media_maintenance FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._inventory_ref(p_ref jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'schema', p_ref->'schema', 'purpose', p_ref->'purpose', 'location_id', p_ref->'location_id',
        'config_revision', p_ref->'config_revision', 'binding_generation', p_ref->'binding_generation',
        'endpoint_url', p_ref->'endpoint_url', 'bucket', p_ref->'bucket', 'object_key', p_ref->'object_key',
        'prefix', p_ref->'prefix', 'key_prefix', p_ref->'key_prefix', 'storage_identity', p_ref->'storage_identity'
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.export_zip_inventory(p_after uuid, p_limit integer)
RETURNS TABLE(id uuid, state text, storage_ref jsonb, sha256 text, size_bytes bigint, ref_present boolean,
              version_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT z.id, z.state::text, yoao_export_v1._inventory_ref(z.storage_ref),
           z.sha256::text, z.size_bytes, z.storage_ref IS NOT NULL, z.upload_proof->>'version_id'
    FROM public.export_outputs z WHERE p_after IS NULL OR z.id > p_after
    ORDER BY z.id LIMIT greatest(1, least(p_limit, 1001));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.export_media_inventory(p_after uuid, p_limit integer)
RETURNS TABLE(id uuid, state text, storage_ref jsonb, ref_present boolean,
              inventory_present boolean, inventory_schema jsonb, inventory_objects_type text,
              input_snapshot_unexpanded boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT b.id, b.state::text, yoao_export_v1._inventory_ref(b.storage_ref), b.storage_ref IS NOT NULL,
           b.inventory IS NOT NULL, b.inventory->'schema', jsonb_typeof(b.inventory->'objects'),
           b.input_snapshot IS NOT NULL
    FROM public.export_media_bundles b WHERE p_after IS NULL OR b.id > p_after
    ORDER BY b.id LIMIT greatest(1, least(p_limit, 1001));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.export_media_inventory_objects(p_after uuid, p_key text, p_limit integer)
RETURNS TABLE(id uuid, state text, storage_ref jsonb, inventory_key text, object_sha256 text,
              object_size jsonb, object_version text, object_state text, ref_present boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT b.id, b.state::text, yoao_export_v1._inventory_ref(b.storage_ref), o.key,
           o.value->>'sha256', o.value->'size_bytes',
           coalesce(v.version_id, o.value->>'version_id'), o.value->>'state',
           b.storage_ref IS NOT NULL
    FROM public.export_media_bundles b CROSS JOIN LATERAL jsonb_each(
        CASE WHEN b.inventory->'schema' = '1'::jsonb AND jsonb_typeof(b.inventory->'objects') = 'object'
             THEN b.inventory->'objects' ELSE '{}'::jsonb END
    ) o
    LEFT JOIN public.export_media_member_versions v ON v.bundle_id = b.id
        AND v.member_id = substring(o.key FROM '/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[.]bin$')::uuid
        AND o.key = (b.storage_ref->>'key_prefix') || v.member_id::text || '.bin'
    WHERE p_after IS NULL OR (b.id >= p_after AND (b.id, o.key COLLATE "C") > (p_after, p_key COLLATE "C"))
    ORDER BY b.id, o.key COLLATE "C" LIMIT greatest(1, least(p_limit, 1001));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.export_zip_usage(p_ids uuid[])
RETURNS TABLE(id uuid, project_id uuid, job_id uuid, media text, storage_ref jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT z.id, z.project_id, z.job_id, z.media::text, z.storage_ref
    FROM public.export_outputs z WHERE cardinality(p_ids) <= 1000 AND z.id = ANY(p_ids);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.export_media_usage(p_ids uuid[])
RETURNS TABLE(id uuid, storage_ref jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT b.id, b.storage_ref FROM public.export_media_bundles b
    WHERE cardinality(p_ids) <= 1000 AND b.id = ANY(p_ids);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.retained_zip_download(p_id uuid)
RETURNS TABLE (id uuid, storage_ref jsonb, sha256 text, size_bytes bigint,
               expires_at timestamptz, version_id text)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT z.id, z.storage_ref, z.sha256::text, z.size_bytes, z.expires_at,
           z.upload_proof->>'version_id'
    FROM public.export_outputs z
    WHERE z.id = p_id AND z.state = 'ready' AND z.expires_at > clock_timestamp();
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_export_v1._matches_export_allocation(jsonb),
    yoao_export_v1.zip_receipt(uuid,text),
    yoao_export_v1.reserve_zip(uuid,uuid,uuid,uuid,text,text,integer,text,bigint,jsonb,timestamptz,timestamptz),
    yoao_export_v1.save_zip_proof(uuid,jsonb), yoao_export_v1.publish_zip(uuid),
    yoao_export_v1.allocate_media(uuid,uuid,uuid,uuid,jsonb,text,jsonb,jsonb,integer,bigint,jsonb,timestamptz,timestamptz),
    yoao_export_v1.media_delivery(uuid,boolean), yoao_export_v1.media_fence(uuid,text),
    yoao_export_v1.claim_media(uuid,uuid,timestamptz),
    yoao_export_v1.publish_media(uuid,uuid,timestamptz,timestamptz),
    yoao_export_v1.save_media_version(uuid,uuid,uuid,timestamptz,text),
    yoao_export_v1.media_version(uuid,uuid), yoao_export_v1.media_versions(uuid),
    yoao_export_v1.find_cache(text,timestamptz), yoao_export_v1.lock_cache(uuid),
    yoao_export_v1.invalidate_cache(uuid,uuid), yoao_export_v1.record_cache(text,uuid,uuid),
    yoao_export_v1._media_has_operations(uuid,uuid,uuid),
    yoao_export_v1._media_attempt_ended(uuid,uuid,uuid,timestamptz,timestamptz,timestamptz,timestamptz),
    yoao_export_v1.media_attempt_ended(uuid,timestamptz), yoao_export_v1.media_has_operations(uuid,uuid),
    yoao_export_v1.lock_media_retirement(uuid,boolean), yoao_export_v1.start_media_retirement(uuid,timestamptz),
    yoao_export_v1.finish_media_retirement(uuid,timestamptz), yoao_export_v1.media_retirement_queue(timestamptz,integer),
    yoao_export_v1.zip_retirement_bundle(uuid), yoao_export_v1._zip_has_read(uuid),
    yoao_export_v1._zip_retirement_allowed(uuid,uuid,timestamptz,boolean),
    yoao_export_v1.start_zip_retirement(uuid,timestamptz), yoao_export_v1.zip_retirement_unreferenced(uuid),
    yoao_export_v1.remove_retired_zip(uuid,timestamptz), yoao_export_v1.purge_legacy_cache(timestamptz,integer),
    yoao_export_v1.zip_retirement_queue(timestamptz,integer), yoao_export_v1.binding_history(),
    yoao_export_v1.saved_location_is_referenced(uuid), yoao_export_v1._inventory_ref(jsonb),
    yoao_export_v1.export_zip_inventory(uuid,integer), yoao_export_v1.export_media_inventory(uuid,integer),
    yoao_export_v1.export_media_inventory_objects(uuid,text,integer),
    yoao_export_v1.export_zip_usage(uuid[]), yoao_export_v1.export_media_usage(uuid[]),
    yoao_export_v1.retained_zip_download(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE VIEW yoao_assets_v1.export_dataset_facts WITH (security_barrier=true) AS
    SELECT d.id, d.display_id, d.name, d.data_type, d.is_temporal, d.lifecycle_revision,
           d.archived_at, d.metadata, octet_length(to_jsonb(d)::text) AS export_row_bytes
    FROM public.datasets d CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
CREATE VIEW yoao_assets_v1.export_item_facts WITH (security_barrier=true) AS
    SELECT i.id, i.dataset_id, i.file_name, i.file_path, i.file_type, i.file_size,
           i.content_hash, i.width, i.height, i.nuscenes_raw_original_id, i.metadata,
           i.media_source_generation, i.media_timetable_revision,
           i.media_materialized_generation, i.media_object_identity, i.media_source_drifted,
           i.scene_id, i.frame_index, i.created_at, i.updated_at, i.thumbnail_output_id,
           octet_length(to_jsonb(i)::text) AS export_row_bytes
    FROM public.dataset_items i CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
CREATE VIEW yoao_assets_v1.export_scene_facts WITH (security_barrier=true) AS
    SELECT s.id, s.display_id, s.dataset_id, s.name, s.nuscenes_map_original_id,
           s.source_format, s.source_metadata, s.created_by, s.created_at, s.updated_at,
           octet_length(to_jsonb(s)::text) AS export_row_bytes
    FROM public.scenes s CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
CREATE VIEW yoao_assets_v1.export_calibration_facts WITH (security_barrier=true) AS
    SELECT c.id, c.dataset_item_id, c.revision, c.digest, c.calibration, c.created_by, c.created_at,
           octet_length(to_jsonb(c)::text) AS export_row_bytes
    FROM public.sensor_calibration_revisions c CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
CREATE VIEW yoao_assets_v1.export_pose_facts WITH (security_barrier=true) AS
    SELECT p.id, p.scene_id, p.frame_index, p.timestamp_us, p.ego_translation, p.ego_rotation,
           p.source_metadata, p.created_at, octet_length(to_jsonb(p)::text) AS export_row_bytes
    FROM public.scene_frame_poses p CROSS JOIN (VALUES (1)) AS read_only(fence);
-- aap-statement-break
REVOKE ALL ON yoao_assets_v1.export_dataset_facts, yoao_assets_v1.export_item_facts,
    yoao_assets_v1.export_scene_facts, yoao_assets_v1.export_calibration_facts,
    yoao_assets_v1.export_pose_facts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_export_datasets_share(p_ids uuid[])
RETURNS TABLE (id uuid) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT d.id FROM public.datasets d
    WHERE cardinality(p_ids) <= 50001 AND d.id = ANY(p_ids)
    ORDER BY d.id FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_export_items_update(p_ids uuid[])
RETURNS TABLE (id uuid) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT i.id FROM public.dataset_items i
    WHERE cardinality(p_ids) <= 50001 AND i.id = ANY(p_ids)
    ORDER BY i.id FOR UPDATE NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_export_scenes_update(p_ids uuid[])
RETURNS TABLE (id uuid) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT s.id FROM public.scenes s
    WHERE cardinality(p_ids) <= 50001 AND s.id = ANY(p_ids)
    ORDER BY s.id FOR UPDATE NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_export_calibrations_share(p_ids uuid[])
RETURNS TABLE (id uuid) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT c.id FROM public.sensor_calibration_revisions c
    WHERE cardinality(p_ids) <= 100001 AND c.id = ANY(p_ids)
    ORDER BY c.id FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_export_poses_share(p_ids uuid[])
RETURNS TABLE (id uuid) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT p.id FROM public.scene_frame_poses p
    WHERE cardinality(p_ids) <= 50001 AND p.id = ANY(p_ids)
    ORDER BY p.id FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_export_datasets_share(uuid[]),
    yoao_assets_v1.lock_export_items_update(uuid[]), yoao_assets_v1.lock_export_scenes_update(uuid[]),
    yoao_assets_v1.lock_export_calibrations_share(uuid[]), yoao_assets_v1.lock_export_poses_share(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1._export_io_gate_revision(
    p_operation uuid, p_location uuid, p_purposes text[], p_current boolean
) RETURNS integer LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT op.revision FROM public.storage_operations op
    WHERE op.id = p_operation AND op.location_id = p_location
      AND op.purpose = ANY(p_purposes)
      AND (NOT p_current OR EXISTS (
          SELECT 1 FROM public.storage_maintenance_gates gate
          WHERE gate.location_id = op.location_id AND gate.state = 'active' AND gate.revision = op.revision
      ));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.zip_io_subject(
    p_zip uuid, p_operation uuid, p_action text
) RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'kind', 'zip', 'action', p_action,
        'phase', CASE WHEN z.state = 'pending' THEN
                     CASE WHEN z.upload_proof IS NULL THEN 'awaiting' ELSE 'verified' END
                     ELSE z.state END,
        'project_id', z.project_id, 'job_id', z.job_id, 'output_id', z.id, 'bundle_id', z.media_bundle_id,
        'storage_ref', z.storage_ref, 'object_key', z.storage_ref->>'object_key',
        'sha256', z.sha256, 'size_bytes', z.size_bytes,
        'version_id', z.upload_proof->>'version_id', 'etag', z.upload_proof->>'etag',
        'created_at', z.created_at, 'expires_at', z.expires_at,
        'operation_id', p_operation, 'gate_revision', permit.revision
    )
    FROM public.export_outputs z
    LEFT JOIN public.export_media_bundles b ON b.id = z.media_bundle_id
    CROSS JOIN LATERAL (
        SELECT yoao_export_v1._export_io_gate_revision(
            p_operation, (z.storage_ref->>'location_id')::uuid, ARRAY[
                'export_media_' || replace(b.id::text, '-', ''),
                CASE p_action
                  WHEN 'write' THEN 'export_write_'
                  WHEN 'delete' THEN 'export_delete_'
                  WHEN 'verify' THEN 'export_read_'
                  WHEN 'read' THEN 'export_read_'
                  WHEN 'sign' THEN 'export_read_'
                END || replace(z.id::text, '-', '')
            ], true
        ) AS revision
    ) permit
    WHERE z.id = p_zip AND permit.revision IS NOT NULL
      AND (z.media_bundle_id IS NULL OR (
          b.id IS NOT NULL AND b.zip_output_id = z.id AND b.project_id = z.project_id
          AND b.job_id = z.job_id AND b.expires_at = z.expires_at
      ))
      AND CASE p_action
          WHEN 'write' THEN z.state = 'pending' AND z.upload_proof IS NULL
            AND z.expires_at > clock_timestamp()
            AND (b.id IS NULL OR (b.state = 'building' AND b.lease_expires_at > clock_timestamp()))
          WHEN 'sign' THEN z.expires_at > clock_timestamp() AND (
            (z.state = 'pending' AND z.upload_proof IS NOT NULL
              AND (b.id IS NULL OR (b.state = 'building' AND b.lease_expires_at > clock_timestamp())))
            OR (z.state = 'ready' AND (b.id IS NULL OR b.state = 'ready'))
          )
          WHEN 'verify' THEN z.state = 'ready' AND z.expires_at > clock_timestamp()
            AND (b.id IS NULL OR b.state = 'ready')
          WHEN 'read' THEN z.state = 'ready' AND z.expires_at > clock_timestamp()
            AND (b.id IS NULL OR b.state = 'ready')
          WHEN 'delete' THEN z.state = 'retired' AND yoao_export_v1.zip_retirement_unreferenced(z.id)
            AND (b.id IS NULL OR NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, p_operation))
          ELSE false
      END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_member_io_subject(
    p_bundle uuid, p_member uuid, p_operation uuid, p_action text
) RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'kind', 'member', 'action', p_action, 'phase', b.state,
        'project_id', b.project_id, 'job_id', b.job_id, 'bundle_id', b.id, 'member_id', p_member,
        'storage_ref', b.storage_ref, 'object_key', member.object_key,
        'sha256', member.receipt->>'sha256', 'size_bytes', member.receipt->'size_bytes',
        'version_id', v.version_id, 'etag', NULL::text,
        'created_at', b.created_at, 'expires_at', b.expires_at,
        'operation_id', p_operation, 'gate_revision', permit.revision
    )
    FROM public.export_media_bundles b
    LEFT JOIN public.export_media_member_versions v ON v.bundle_id = b.id AND v.member_id = p_member
    CROSS JOIN LATERAL (
        SELECT (b.storage_ref->>'key_prefix') || p_member::text || '.bin' AS object_key,
            b.inventory->'objects'->((b.storage_ref->>'key_prefix') || p_member::text || '.bin') AS receipt
    ) member
    CROSS JOIN LATERAL (
        SELECT yoao_export_v1._export_io_gate_revision(
            p_operation, (b.storage_ref->>'location_id')::uuid,
            ARRAY['export_media_' || replace(b.id::text, '-', '')], true
        ) AS revision
    ) permit
    WHERE b.id = p_bundle AND member.receipt IS NOT NULL AND permit.revision IS NOT NULL
      AND CASE p_action
          WHEN 'write' THEN b.state = 'building' AND b.lease_expires_at > clock_timestamp()
            AND v.version_id IS NULL
          WHEN 'read' THEN b.expires_at > clock_timestamp()
            AND (b.state = 'ready' OR (b.state = 'building' AND b.lease_expires_at > clock_timestamp()))
          WHEN 'sign' THEN b.expires_at > clock_timestamp()
            AND (b.state = 'ready' OR (b.state = 'building' AND b.lease_expires_at > clock_timestamp()))
          WHEN 'verify' THEN b.state = 'ready' AND b.expires_at > clock_timestamp()
          WHEN 'delete' THEN b.state = 'retiring'
            AND NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, p_operation)
          ELSE false
      END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.media_prefix_io_subject(
    p_bundle uuid, p_operation uuid, p_action text
) RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'kind', 'prefix', 'action', p_action, 'phase', b.state,
        'project_id', b.project_id, 'job_id', b.job_id, 'bundle_id', b.id,
        'storage_ref', b.storage_ref, 'key_prefix', b.storage_ref->>'key_prefix',
        'inventory', b.inventory,
        'member_versions', COALESCE((
            SELECT jsonb_object_agg((b.storage_ref->>'key_prefix') || v.member_id::text || '.bin', v.version_id)
            FROM public.export_media_member_versions v WHERE v.bundle_id = b.id
        ), '{}'::jsonb),
        'created_at', b.created_at, 'expires_at', b.expires_at,
        'operation_id', p_operation, 'gate_revision', permit.revision
    )
    FROM public.export_media_bundles b
    CROSS JOIN LATERAL (
        SELECT yoao_export_v1._export_io_gate_revision(
            p_operation, (b.storage_ref->>'location_id')::uuid,
            ARRAY['export_media_' || replace(b.id::text, '-', '')], true
        ) AS revision
    ) permit
    WHERE b.id = p_bundle AND permit.revision IS NOT NULL AND p_action IN ('list', 'list_versions')
      AND (
          p_action = 'list' AND b.state = 'ready' AND b.expires_at > clock_timestamp()
          OR b.state = 'retiring' AND NOT yoao_export_v1._media_has_operations(b.id, b.zip_output_id, p_operation)
      );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_export_v1._export_io_gate_revision(uuid,uuid,text[],boolean),
    yoao_export_v1.zip_io_subject(uuid,uuid,text), yoao_export_v1.media_member_io_subject(uuid,uuid,uuid,text),
    yoao_export_v1.media_prefix_io_subject(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.save_source_zip_proof(
    p_zip uuid, p_operation uuid, p_proof jsonb
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE public.export_outputs z SET upload_proof = p_proof
    WHERE z.id = p_zip AND z.state = 'pending' AND z.upload_proof IS NULL
      AND p_proof IS NOT NULL
      AND yoao_export_v1._export_io_gate_revision(
          p_operation, (z.storage_ref->>'location_id')::uuid, ARRAY[
              'export_media_' || replace(z.media_bundle_id::text, '-', ''),
              'export_write_' || replace(z.id::text, '-', '')
          ], false
      ) IS NOT NULL;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_export_v1.save_source_media_version(
    p_bundle uuid, p_member uuid, p_operation uuid, p_version text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_token uuid;
    v_expires timestamptz;
BEGIN
    SELECT b.lease_token, b.lease_expires_at INTO v_token, v_expires
    FROM public.export_media_bundles b
    WHERE b.id = p_bundle AND yoao_export_v1._export_io_gate_revision(
        p_operation, (b.storage_ref->>'location_id')::uuid,
        ARRAY['export_media_' || replace(b.id::text, '-', '')], false
    ) IS NOT NULL
    FOR UPDATE;
    IF NOT FOUND OR p_version IS NULL THEN
        RETURN false;
    END IF;
    RETURN yoao_export_v1.save_media_version(p_bundle, p_member, v_token, v_expires, p_version);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_export_v1.save_source_zip_proof(uuid,uuid,jsonb),
    yoao_export_v1.save_source_media_version(uuid,uuid,uuid,text) FROM PUBLIC;
