CREATE FUNCTION yoao_assets_v1.matches_avatar_reservation(p_token text, p_storage_ref jsonb)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH target AS (
        SELECT binding.location_id, binding.generation, binding.bucket,
               location.config_revision,
               location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid AS legacy,
               CASE WHEN location.connection_id IS NULL THEN location.config
                    ELSE connection.config END AS config
        FROM public.storage_bindings AS binding
        JOIN public.storage_locations AS location ON location.id = binding.location_id
        LEFT JOIN public.storage_connections AS connection ON connection.id = location.connection_id
        WHERE binding.purpose = 'avatars' AND location.kind = 's3' AND location.enabled
          AND (
            (location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
             AND location.connection_id IS NULL AND location.config_revision = 1)
            OR
            (location.id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
             AND binding.generation > 1 AND connection.kind = 's3'
             AND connection.scope = 'global' AND connection.project_id IS NULL
             AND connection.config->>'bucket' = binding.bucket)
          )
    ), address AS (
        SELECT target.*,
               CASE WHEN strpos(config->>'endpoint', '://') > 0
                    THEN rtrim(config->>'endpoint', '/')
                    ELSE (CASE WHEN config->'use_ssl' = 'true'::jsonb
                               THEN 'https://' ELSE 'http://' END) || (config->>'endpoint')
               END AS endpoint,
               CASE WHEN legacy THEN p_token || '.webp'
                    ELSE concat_ws('/', nullif(regexp_replace(config->>'base_prefix', '/$', ''), ''),
                                  '_aap_managed', 'avatars', p_token || '.webp')
               END AS object_key
        FROM target
    )
    SELECT EXISTS (
        SELECT FROM address
        WHERE jsonb_build_object(
            'schema', CASE WHEN address.generation = 1 THEN 1 ELSE 2 END,
            'purpose', 'avatars', 'location_id', address.location_id,
            'config_revision', address.config_revision, 'binding_generation', address.generation,
            'endpoint_url', address.endpoint, 'bucket', address.bucket, 'object_key', address.object_key,
            'storage_identity', encode(sha256(convert_to(
                jsonb_build_array(address.endpoint, address.bucket)::text, 'UTF8'
            )), 'hex')
        ) = p_storage_ref
    )
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.reserve_avatar_upload(
    p_actor_id uuid, p_token text, p_storage_ref jsonb, p_sha256 text, p_size_bytes integer
)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT FROM yoao_identity_v1.read_actor_facts(p_actor_id) AS actor
        WHERE actor.is_active
    ) THEN
        RETURN 'avatar_authority_revoked';
    END IF;

    INSERT INTO public.avatar_uploads (token, owner_id, storage_ref, sha256, size_bytes)
    SELECT p_token, p_actor_id, p_storage_ref, p_sha256, p_size_bytes
    WHERE yoao_assets_v1.matches_avatar_reservation(p_token, p_storage_ref);
    IF NOT FOUND THEN
        RETURN 'avatar_storage_binding_unproven';
    END IF;
    RETURN 'reserved';
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.confirm_avatar_upload_version(
    p_actor_id uuid, p_token text, p_storage_ref jsonb, p_sha256 text,
    p_size_bytes integer, p_object_version_id text
)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH saved AS (
        UPDATE public.avatar_uploads AS upload
        SET object_version_id = p_object_version_id
        WHERE upload.token = p_token AND upload.owner_id = p_actor_id
          AND upload.state = 'pending' AND upload.object_version_id IS NULL
          AND upload.storage_ref = p_storage_ref AND upload.sha256 = p_sha256
          AND upload.size_bytes = p_size_bytes AND p_object_version_id IS NOT NULL
        RETURNING 1
    )
    SELECT EXISTS (SELECT FROM saved)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.avatar_upload_pending_matches(
    p_actor_id uuid, p_token text, p_storage_ref jsonb, p_sha256 text,
    p_size_bytes integer, p_object_version_id text
)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.avatar_uploads AS upload
        WHERE upload.token = p_token AND upload.owner_id = p_actor_id
          AND upload.state = 'pending'
          AND upload.storage_ref = p_storage_ref AND upload.sha256 = p_sha256
          AND upload.size_bytes = p_size_bytes
          AND upload.object_version_id IS NOT DISTINCT FROM p_object_version_id
    )
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.read_ready_avatar_upload(p_token text)
RETURNS TABLE(token text, storage_ref jsonb, sha256 text, size_bytes integer, object_version_id text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT upload.token::text, upload.storage_ref, upload.sha256::text,
           upload.size_bytes, upload.object_version_id::text
    FROM public.avatar_uploads AS upload
    WHERE upload.token = p_token AND upload.state = 'ready'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.list_avatar_retirements(p_limit integer)
RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT upload.token::text
    FROM public.avatar_uploads AS upload
    WHERE upload.state = 'retired'
    ORDER BY upload.last_attempted_at ASC NULLS FIRST, upload.created_at, upload.token
    LIMIT greatest(1, least(p_limit, 100))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.begin_avatar_retirement(p_token text)
RETURNS TABLE(token text, storage_ref jsonb, sha256 text, size_bytes integer, object_version_id text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM FROM public.avatar_uploads AS upload
    WHERE upload.token = p_token AND upload.state = 'retired'
    FOR UPDATE SKIP LOCKED;
    IF NOT FOUND OR EXISTS (
        SELECT FROM yoao_identity_v1.account_profiles AS account
        WHERE account.avatar_ref = 'upload:' || p_token
    ) THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.avatar_uploads AS upload
    SET last_attempted_at = clock_timestamp()
    WHERE upload.token = p_token
    RETURNING upload.token::text, upload.storage_ref, upload.sha256::text,
              upload.size_bytes, upload.object_version_id::text;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.finish_avatar_retirement(
    p_token text, p_storage_ref jsonb, p_sha256 text, p_size_bytes integer, p_object_version_id text
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM FROM public.avatar_uploads AS upload
    WHERE upload.token = p_token AND upload.state = 'retired'
      AND upload.storage_ref = p_storage_ref AND upload.sha256 = p_sha256
      AND upload.size_bytes = p_size_bytes
      AND upload.object_version_id IS NOT DISTINCT FROM p_object_version_id
    FOR UPDATE;
    IF NOT FOUND OR EXISTS (
        SELECT FROM yoao_identity_v1.account_profiles AS account
        WHERE account.avatar_ref = 'upload:' || p_token
    ) THEN
        RETURN false;
    END IF;
    DELETE FROM public.avatar_uploads AS upload WHERE upload.token = p_token;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.read_avatar_binding_history()
RETURNS TABLE(pending bigint, ready bigint, retired bigint, unlocated_users bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FILTER (WHERE upload.state = 'pending'),
           count(*) FILTER (WHERE upload.state = 'ready'),
           count(*) FILTER (WHERE upload.state = 'retired'),
           (SELECT count(*)
            FROM yoao_identity_v1.account_profiles AS account
            LEFT JOIN public.avatar_uploads AS matched ON matched.token = substr(account.avatar_ref, 8)
            WHERE account.avatar_ref LIKE 'upload:%'
              AND (matched.token IS NULL OR matched.owner_id <> account.id OR matched.state <> 'ready'))
    FROM public.avatar_uploads AS upload
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.avatar_saved_target_referenced(p_location_id uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.avatar_uploads AS upload
        WHERE upload.storage_ref->>'location_id' = p_location_id::text
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.matches_avatar_reservation(text,jsonb),
    yoao_identity_v1.reserve_avatar_upload(uuid,text,jsonb,text,integer),
    yoao_identity_v1.confirm_avatar_upload_version(uuid,text,jsonb,text,integer,text),
    yoao_identity_v1.avatar_upload_pending_matches(uuid,text,jsonb,text,integer,text),
    yoao_identity_v1.read_ready_avatar_upload(text),
    yoao_identity_v1.list_avatar_retirements(integer),
    yoao_identity_v1.begin_avatar_retirement(text),
    yoao_identity_v1.finish_avatar_retirement(text,jsonb,text,integer,text),
    yoao_identity_v1.read_avatar_binding_history(),
    yoao_identity_v1.avatar_saved_target_referenced(uuid)
FROM PUBLIC;
