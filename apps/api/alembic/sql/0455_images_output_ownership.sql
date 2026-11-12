-- A native pyramid attempt owns its real saved output before any physical write.
-- The API admission keeps the authenticated Task/API-key proof; actor_id alone
-- is not an admission.  Every real caller of these two functions is the public
-- Task image-pyramid route, so HTTP Task admission is the only accepted mode.
-- DatasetItem and system callers keep using prepare_image_media/retry_image_media.
CREATE FUNCTION aap_assets_v1._accept_owned_pyramid(
    p_operation_id uuid, p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid,
    p_purpose text, p_expected_source_snapshot text, p_policy jsonb,
    p_force boolean, p_delegation jsonb, p_action text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_original jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_action text := p_action;
    v_force boolean := p_force;
    v_saved_action text;
    v_saved_force boolean;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_task_id IS NULL
       OR p_dataset_item_id IS NOT NULL OR p_purpose IS DISTINCT FROM 'pyramid'
       OR p_action NOT IN ('request', 'retry') OR p_force IS NULL
       OR (p_expected_source_snapshot IS NOT NULL
           AND p_expected_source_snapshot !~ '^[0-9a-f]{64}$')
       OR pg_catalog.jsonb_typeof(p_delegation) IS DISTINCT FROM 'object'
       OR (p_delegation - ARRAY[
              'schema','mode','owner_kind','owner_id','original','task_binding',
              'task_id','actor_id','api_key_id','api_key_fingerprint'
          ]) <> '{}'::jsonb
       OR NOT (p_delegation ?& ARRAY[
              'schema','mode','owner_kind','owner_id','original','task_binding',
              'task_id','actor_id','api_key_id','api_key_fingerprint'
          ])
       OR p_delegation -> 'schema' IS DISTINCT FROM '1'::jsonb
       OR p_delegation ->> 'mode' IS DISTINCT FROM 'http'
       OR p_delegation ->> 'owner_kind' IS DISTINCT FROM 'task'
       OR p_delegation ->> 'owner_id' IS DISTINCT FROM p_task_id::text
       OR p_delegation ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR p_delegation ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR (p_delegation -> 'api_key_id' <> 'null'::jsonb AND (
              pg_catalog.jsonb_typeof(p_delegation -> 'api_key_id') IS DISTINCT FROM 'string'
              OR p_delegation ->> 'api_key_id'
                 !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
       OR COALESCE(p_delegation ->> 'api_key_fingerprint','') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_delegation -> 'original') IS DISTINCT FROM 'object'
       OR (p_delegation -> 'task_binding' <> 'null'::jsonb AND (
              pg_catalog.jsonb_typeof(p_delegation -> 'task_binding') IS DISTINCT FROM 'string'
              OR COALESCE(p_delegation ->> 'task_binding','') !~ '^[0-9a-f]{64}$')) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_original := p_delegation -> 'original';
    IF (v_original - ARRAY[
            'schema','owner_kind','content_id','sha256','size_bytes','location_id',
            'location_revision','maintenance_revision','binding'
        ]) <> '{}'::jsonb
       OR NOT (v_original ?& ARRAY[
            'schema','owner_kind','content_id','sha256','size_bytes','location_id',
            'location_revision','maintenance_revision','binding'
        ])
       OR v_original -> 'schema' IS DISTINCT FROM '1'::jsonb
       OR v_original ->> 'owner_kind' NOT IN ('dataset_asset','task_original')
       OR COALESCE(v_original ->> 'content_id','')
          !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(v_original ->> 'sha256','') !~ '^[0-9a-f]{64}$'
       OR COALESCE(v_original ->> 'binding','') !~ '^[0-9a-f]{64}$'
       OR COALESCE(v_original ->> 'location_id','')
          !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_assets_v1._image_exec_integer_valid(
              v_original -> 'size_bytes',1,2147483647)
       OR NOT aap_assets_v1._image_exec_integer_valid(
              v_original -> 'location_revision',1,2147483647)
       OR NOT aap_assets_v1._image_exec_integer_valid(
              v_original -> 'maintenance_revision',1,2147483647) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.action,(operation.intent ->> 'force')::boolean
    INTO v_saved_action,v_saved_force
    FROM public.asset_image_operations AS operation
    WHERE operation.operation_id=p_operation_id AND operation.actor_id=p_actor_id;
    IF p_action='retry' AND v_saved_action='request' AND v_saved_force THEN
        v_action := 'request';
        v_force := true;
    END IF;
    -- A ready generation without ownership evidence is unavailable, never stale.
    -- Retain its facts and admit one fresh recorded generation instead.
    IF p_action='retry' AND v_saved_action IS NULL THEN
        v_reply := aap_assets_v1._image_actor(p_actor_id, true);
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
        v_reply := aap_assets_v1._image_owner_source_share(p_actor_id,p_task_id,NULL,true);
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
        v_source := v_reply -> 'data';
        IF aap_assets_v1._image_source_data_valid(v_source) IS DISTINCT FROM true THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        -- The API-captured original must still describe the observed source.
        IF (v_source ->> 'content_hash' ~ '^[0-9a-f]{64}$'
            AND (v_source ->> 'content_hash') IS DISTINCT FROM (v_original ->> 'sha256'))
           OR (v_source -> 'file_size' <> 'null'::jsonb
               AND v_source ->> 'file_size' IS NOT NULL
               AND (v_source ->> 'file_size')::bigint
                   IS DISTINCT FROM (v_original ->> 'size_bytes')::bigint)
           OR (v_source #>> '{object_identity,byte_size}' IS NOT NULL
               AND (v_source #>> '{object_identity,byte_size}')::bigint
                   IS DISTINCT FROM (v_original ->> 'size_bytes')::bigint) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        v_projection := aap_assets_v1._image_pyramid_projection(
            v_source,NULL,COALESCE(p_policy ->> 'profile_version','pyramid-v1')
        );
        IF v_projection ->> 'status'='ready' THEN
            PERFORM 1 FROM public.image_pyramid_assets AS asset
            JOIN public.image_pyramid_generations AS generation
              ON generation.asset_id=asset.id AND generation.generation=asset.active_generation
            WHERE asset.id=(v_projection ->> 'asset_id')::uuid
              AND generation.generation=(v_projection ->> 'generation')::integer
              AND generation.status='ready' AND generation.output_id IS NULL
            FOR UPDATE OF asset,generation NOWAIT;
            IF FOUND THEN v_action := 'request'; v_force := true; END IF;
        END IF;
    END IF;
    v_reply := aap_assets_v1._image_accept_source(
        p_operation_id, p_actor_id, p_task_id, NULL, p_purpose,
        p_expected_source_snapshot, p_policy, v_force, v_action
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
    WHERE job.job_id = (v_reply #>> '{data,work,job_id}')::uuid FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.kind IS DISTINCT FROM 'pyramid'
       OR v_job.generation IS DISTINCT FROM (v_reply #>> '{data,work,generation}')::bigint
       OR v_job.pyramid_asset_id IS NULL OR v_job.pyramid_generation IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='owned_pyramid_job_changed';
    END IF;
    SELECT generation.* INTO v_generation FROM public.image_pyramid_generations AS generation
    WHERE generation.asset_id=v_job.pyramid_asset_id
      AND generation.generation=v_job.pyramid_generation FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_generation.job_id IS DISTINCT FROM v_job.job_id
       OR v_generation.job_generation IS DISTINCT FROM v_job.generation
       OR v_generation.source_generation IS DISTINCT FROM v_job.source_generation
       OR v_generation.source_snapshot IS DISTINCT FROM v_job.source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='owned_pyramid_generation_changed';
    END IF;
    IF v_generation.auth_ref IS NULL THEN
        IF v_generation.status NOT IN ('pending','building') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='owned_pyramid_proof_missing';
        END IF;
        UPDATE public.image_pyramid_generations
        SET original_ref=v_original, auth_ref=p_delegation
        WHERE asset_id=v_generation.asset_id AND generation=v_generation.generation;
    ELSIF v_generation.original_ref IS DISTINCT FROM v_original THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='owned_pyramid_source_changed';
    END IF;
    RETURN v_reply;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accept_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_owned_pyramid(
    uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb
) RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_assets_v1._accept_owned_pyramid($1,$2,$3,$4,$5,$6,$7,$8,$9,'request')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retry_owned_pyramid(
    uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb
) RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_assets_v1._accept_owned_pyramid($1,$2,$3,$4,$5,$6,$7,$8,$9,'retry')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retry_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb) FROM PUBLIC;
-- aap-statement-break
-- The saved output namespace is a real read scope for the current owner.
CREATE OR REPLACE FUNCTION aap_assets_v1._image_garbage_prefix_valid(p_prefix text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(
        p_prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/(g[1-9][0-9]*/(a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/)?)?$'
        OR p_prefix ~ '^image-pyramids/objects/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'
        OR p_prefix ~ '^image-assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$',
        false
    )
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.read_image_generation(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid,
    p_generation integer, p_profile_version text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_reply jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_selected_profile text;
    v_asset_id uuid;
    v_active_generation integer;
    v_last_accessed_at timestamptz;
    v_generation_policy jsonb;
    v_policy jsonb;
    v_accessed_at timestamptz;
    v_retention_until timestamptz;
    v_asset_prefix text;
    v_generation_prefix text;
    v_attempt_prefix text;
    v_prefix text;
    v_prefixes text[];
    v_garbage public.asset_image_garbage%ROWTYPE;
    v_garbage_error text;
    v_touch_access boolean;
BEGIN
    IF p_actor_id IS NULL OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR (p_generation IS NOT NULL AND p_generation < 1)
       OR (p_profile_version IS NOT NULL
           AND pg_catalog.char_length(p_profile_version) NOT BETWEEN 1 AND 64) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_selected_profile := COALESCE(p_profile_version, 'pyramid-v1');
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_projection := aap_assets_v1._image_pyramid_projection(
        v_source, p_generation, v_selected_profile
    );
    IF v_projection IS NULL THEN
        RETURN aap_assets_v1._image_reply(
            NULL, CASE WHEN p_generation IS NOT NULL THEN 'asset_missing' ELSE 'invalid_response' END
        );
    END IF;
    IF p_generation IS NOT NULL
       AND v_projection ->> 'status' = 'ready'
       AND v_projection ->> 'generation' = p_generation::text THEN
        BEGIN
            v_asset_id := (v_projection ->> 'asset_id')::uuid;
            SELECT asset.active_generation, generation.last_accessed_at,
                   generation.policy
            INTO v_active_generation, v_last_accessed_at, v_generation_policy
            FROM public.image_pyramid_assets AS asset
            JOIN public.image_pyramid_generations AS generation
              ON generation.asset_id = asset.id
            WHERE asset.id = v_asset_id
              AND asset.profile_version = v_selected_profile
              AND generation.generation = p_generation
              AND generation.status = 'ready'
              AND generation.source_generation = (v_source ->> 'source_generation')::bigint
              AND generation.source_snapshot = v_source ->> 'source_snapshot'
              AND generation.source_identity = aap_assets_v1._image_exec_legacy_identity(
                    v_source -> 'object_identity'
              )
              AND (v_source -> 'width' = 'null'::jsonb
                   OR generation.width = (v_source ->> 'width')::integer)
              AND (v_source -> 'height' = 'null'::jsonb
                   OR generation.height = (v_source ->> 'height')::integer)
            FOR UPDATE OF asset, generation NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
            END IF;

            v_policy := aap_assets_v1._image_policy(
                COALESCE(v_generation_policy, '{}'::jsonb)
            );
            IF v_policy IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_asset_prefix := 'image-pyramids/' || v_asset_id::text || '/';
            v_generation_prefix := v_asset_prefix || 'g' || p_generation::text || '/';
            v_attempt_prefix := pg_catalog.regexp_replace(
                v_projection ->> 'manifest_key', 'manifest\.json$', ''
            );
            IF v_attempt_prefix IS NULL
               OR NOT (
                   v_attempt_prefix = v_generation_prefix
                   OR (pg_catalog.left(v_attempt_prefix, pg_catalog.char_length(v_generation_prefix))
                           = v_generation_prefix
                       AND aap_assets_v1._image_garbage_prefix_valid(v_attempt_prefix))
                   OR (pg_catalog.left(v_attempt_prefix,
                           pg_catalog.char_length('image-pyramids/objects/'))
                           = 'image-pyramids/objects/'
                       AND aap_assets_v1._image_garbage_prefix_valid(v_attempt_prefix))
               ) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_prefixes := ARRAY[v_asset_prefix, v_generation_prefix, v_attempt_prefix];
            v_touch_access := v_last_accessed_at IS NULL
                OR v_last_accessed_at < pg_catalog.clock_timestamp() - interval '10 minutes';
            IF v_touch_access THEN
                v_accessed_at := pg_catalog.clock_timestamp();
            ELSE
                v_accessed_at := v_last_accessed_at;
            END IF;
            v_retention_until := v_accessed_at + pg_catalog.make_interval(
                hours => (v_policy ->> 'gc_grace_hours')::integer
            );
            BEGIN
                FOR v_prefix IN
                    SELECT DISTINCT candidate.prefix
                    FROM pg_catalog.unnest(v_prefixes) AS candidate(prefix)
                    ORDER BY candidate.prefix
                LOOP
                    IF NOT pg_catalog.pg_try_advisory_xact_lock(
                        pg_catalog.hashtextextended('aap-image-garbage:' || v_prefix, 0)
                    ) THEN
                        v_garbage_error := 'operation_busy';
                        RAISE lock_not_available USING MESSAGE = 'image garbage scope is busy';
                    END IF;
                END LOOP;
                FOR v_garbage IN
                    SELECT garbage.*
                    FROM public.asset_image_garbage AS garbage
                    WHERE garbage.prefix = ANY(v_prefixes)
                    ORDER BY garbage.prefix
                    FOR UPDATE NOWAIT
                LOOP
                    IF v_garbage.completed_at IS NOT NULL
                       OR v_garbage.last_ack_token IS NOT NULL THEN
                        v_garbage_error := 'asset_missing';
                        RAISE lock_not_available USING MESSAGE = 'image generation was collected';
                    ELSIF v_garbage.lease_token IS NOT NULL THEN
                        v_garbage_error := 'source_busy';
                        RAISE lock_not_available USING MESSAGE = 'image generation cleanup is active';
                    END IF;
                END LOOP;
                IF v_touch_access THEN
                    UPDATE public.image_pyramid_generations AS generation
                    SET last_accessed_at = v_accessed_at
                    WHERE generation.asset_id = v_asset_id
                      AND generation.generation = p_generation;
                END IF;
                IF v_active_generation IS DISTINCT FROM p_generation THEN
                    PERFORM aap_assets_v1._queue_image_garbage(
                        v_generation_prefix, 'generation_retired', v_retention_until
                    );
                END IF;
                UPDATE public.asset_image_garbage AS garbage
                SET not_before = GREATEST(garbage.not_before, v_retention_until)
                WHERE garbage.prefix = ANY(v_prefixes)
                  AND garbage.completed_at IS NULL
                  AND garbage.lease_token IS NULL
                  AND garbage.not_before < v_retention_until;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._image_reply(
                    NULL, COALESCE(v_garbage_error, 'operation_busy')
                );
            END;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('source', v_source, 'pyramid', v_projection), NULL
    );
END;
$function$;
-- aap-statement-break
-- Native admission records its API proof once on the fresh generation.  Once
-- recorded, the input and authority receipts stay immutable.
CREATE OR REPLACE FUNCTION guard_image_pyramid_inputs()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF ROW(NEW.original_ref,NEW.auth_ref) IS DISTINCT FROM ROW(OLD.original_ref,OLD.auth_ref) THEN
        IF NOT (
            OLD.original_ref IS NULL AND OLD.auth_ref IS NULL
            AND OLD.output_id IS NULL
            AND OLD.status IN ('pending','building')
            AND NEW.status IN ('pending','building')
            AND pg_catalog.jsonb_typeof(NEW.original_ref) = 'object'
            AND pg_catalog.jsonb_typeof(NEW.auth_ref) = 'object'
            AND OLD.job_id IS NOT NULL
            AND NEW.job_id IS NOT DISTINCT FROM OLD.job_id
            AND EXISTS (
                SELECT 1 FROM public.asset_image_jobs AS job
                WHERE job.job_id = OLD.job_id
                  AND job.pyramid_asset_id = OLD.asset_id
                  AND job.pyramid_generation = OLD.generation
                  AND job.status IN ('pending','running')
                  AND job.actor_id = (NEW.auth_ref ->> 'actor_id')::uuid
            )
        ) THEN
            RAISE EXCEPTION 'Pyramid input and authority receipts are immutable'
                USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_inputs_immutable';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION guard_image_pyramid_inputs() FROM PUBLIC;
-- aap-statement-break
-- The read owner accepts the saved-output prefix as a real attempt scope.
DROP TRIGGER IF EXISTS trg_image_pyramid_inputs ON public.image_pyramid_generations;
-- aap-statement-break
CREATE TRIGGER trg_image_pyramid_inputs
BEFORE UPDATE ON public.image_pyramid_generations
FOR EACH ROW EXECUTE FUNCTION guard_image_pyramid_inputs();
-- aap-statement-break
-- One native lease owns exactly one saved output row.  This binding never
-- allocates a second physical owner nor replaces image_pyramid_outputs.
CREATE TABLE aap_assets_v1.image_pyramid_native_attempts (
    output_id uuid PRIMARY KEY REFERENCES public.image_pyramid_outputs(id) ON DELETE CASCADE,
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation > 0),
    lease_token uuid NOT NULL,
    asset_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    source_generation bigint NOT NULL CHECK (source_generation > 0),
    source_snapshot text NOT NULL CHECK (source_snapshot ~ '^[0-9a-f]{64}$'),
    profile_version text NOT NULL CHECK (char_length(profile_version) BETWEEN 1 AND 64),
    gate_revision integer NOT NULL CHECK (gate_revision > 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (job_id,job_generation,lease_token)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.image_pyramid_native_attempts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_image_pyramid_native_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='image_pyramid_attempt_immutable';
    END IF;
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_image_pyramid_native_attempt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER image_pyramid_native_attempt_immutable BEFORE UPDATE
ON aap_assets_v1.image_pyramid_native_attempts
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_image_pyramid_native_attempt();
-- aap-statement-break
-- A member operation is reserved before its only physical PUT.  A submission
-- without a returned receipt stays open and can never be treated as written.
CREATE TABLE aap_assets_v1.image_pyramid_native_members (
    operation_id uuid PRIMARY KEY,
    output_id uuid NOT NULL REFERENCES aap_assets_v1.image_pyramid_native_attempts(output_id) ON DELETE CASCADE,
    relative_key text NOT NULL CHECK (
        relative_key IN ('manifest.json','overview.webp')
        OR relative_key ~ '^tiles/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)[.]webp$'
    ),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    state text NOT NULL CHECK (state IN ('prepared','submitted','written','verified','not_written','unknown')),
    operation_permit uuid,
    etag text,
    version_id text,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (output_id,relative_key),
    CHECK (
        (state='prepared' AND operation_permit IS NOT NULL AND etag IS NULL AND version_id IS NULL)
        OR (state IN ('submitted','not_written') AND operation_permit IS NOT NULL AND etag IS NULL AND version_id IS NULL)
        OR (state IN ('written','verified') AND operation_permit IS NOT NULL
            AND etag IS NOT NULL AND etag <> '' AND (version_id IS NULL OR version_id <> ''))
        OR (state='unknown' AND operation_permit IS NOT NULL
            AND ((etag IS NULL AND version_id IS NULL)
                 OR (etag IS NOT NULL AND etag <> '' AND (version_id IS NULL OR version_id <> ''))))
    )
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.image_pyramid_native_members FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_image_pyramid_native_member()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF (NEW.operation_id,NEW.output_id,NEW.relative_key,NEW.sha256,NEW.size_bytes,NEW.created_at)
           IS DISTINCT FROM
       (OLD.operation_id,OLD.output_id,OLD.relative_key,OLD.sha256,OLD.size_bytes,OLD.created_at)
       OR NEW.operation_permit IS DISTINCT FROM OLD.operation_permit
       OR NOT (
           (OLD.state='prepared' AND NEW.state IN ('submitted','not_written','unknown'))
           OR (OLD.state='submitted' AND NEW.state IN ('written','not_written','unknown'))
           OR (OLD.state='written' AND NEW.state IN ('verified','unknown'))
       ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='image_pyramid_member_immutable';
    END IF;
    IF OLD.etag IS NOT NULL AND (NEW.etag,NEW.version_id) IS DISTINCT FROM (OLD.etag,OLD.version_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='image_pyramid_member_receipt_changed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_image_pyramid_native_member() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER image_pyramid_native_member_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.image_pyramid_native_members
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_image_pyramid_native_member();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._native_pyramid_attempt(p_output uuid, p_lock boolean)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_attempt aap_assets_v1.image_pyramid_native_attempts%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_gate record;
BEGIN
    IF p_output IS NULL OR p_lock IS NULL THEN RETURN NULL; END IF;
    IF p_lock THEN
        SELECT * INTO v_attempt FROM aap_assets_v1.image_pyramid_native_attempts
        WHERE output_id=p_output FOR UPDATE NOWAIT;
    ELSE
        SELECT * INTO v_attempt FROM aap_assets_v1.image_pyramid_native_attempts
        WHERE output_id=p_output;
    END IF;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF p_lock THEN
        SELECT * INTO v_job FROM public.asset_image_jobs WHERE job_id=v_attempt.job_id FOR UPDATE NOWAIT;
    ELSE
        SELECT * INTO v_job FROM public.asset_image_jobs WHERE job_id=v_attempt.job_id;
    END IF;
    IF NOT FOUND OR v_job.kind<>'pyramid' OR v_job.status<>'running'
       OR v_job.generation<>v_attempt.job_generation
       OR v_job.lease_token IS DISTINCT FROM v_attempt.lease_token
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_job.pyramid_asset_id IS DISTINCT FROM v_attempt.asset_id
       OR v_job.pyramid_generation IS DISTINCT FROM v_attempt.generation
       OR v_job.source_generation IS DISTINCT FROM v_attempt.source_generation
       OR v_job.source_snapshot IS DISTINCT FROM v_attempt.source_snapshot THEN RETURN NULL; END IF;
    IF p_lock THEN
        SELECT * INTO v_asset FROM public.image_pyramid_assets WHERE id=v_attempt.asset_id FOR UPDATE NOWAIT;
    ELSE
        SELECT * INTO v_asset FROM public.image_pyramid_assets WHERE id=v_attempt.asset_id;
    END IF;
    IF NOT FOUND OR v_asset.building_generation IS DISTINCT FROM v_attempt.generation
       OR v_asset.profile_version IS DISTINCT FROM v_attempt.profile_version
       OR (CASE v_job.owner_kind WHEN 'dataset_item' THEN v_asset.dataset_item_id
           WHEN 'task' THEN v_asset.task_id ELSE NULL END) IS DISTINCT FROM v_job.owner_id
    THEN RETURN NULL; END IF;
    IF p_lock THEN
        SELECT * INTO v_generation FROM public.image_pyramid_generations
        WHERE asset_id=v_attempt.asset_id AND generation=v_attempt.generation FOR UPDATE NOWAIT;
        SELECT * INTO v_output FROM public.image_pyramid_outputs
        WHERE id=p_output FOR UPDATE NOWAIT;
    ELSE
        SELECT * INTO v_generation FROM public.image_pyramid_generations
        WHERE asset_id=v_attempt.asset_id AND generation=v_attempt.generation;
        SELECT * INTO v_output FROM public.image_pyramid_outputs WHERE id=p_output;
    END IF;
    IF NOT FOUND OR v_generation.status<>'building'
       OR v_generation.job_id IS DISTINCT FROM v_attempt.job_id
       OR v_generation.job_generation IS DISTINCT FROM v_attempt.job_generation
       OR v_generation.lease_token IS DISTINCT FROM v_attempt.lease_token
       OR v_generation.lease_expires_at IS NULL
       OR v_generation.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_generation.source_generation IS DISTINCT FROM v_attempt.source_generation
       OR v_generation.source_snapshot IS DISTINCT FROM v_attempt.source_snapshot
       OR v_output.asset_id IS DISTINCT FROM v_attempt.asset_id
       OR v_output.generation IS DISTINCT FROM v_attempt.generation
       OR v_output.lease_token IS DISTINCT FROM v_attempt.lease_token
       OR v_output.state NOT IN ('building','ready') THEN RETURN NULL; END IF;
    SELECT gate.state,gate.revision INTO v_gate FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id=(v_output.storage_ref ->> 'location_id')::uuid;
    IF NOT FOUND OR v_gate.state<>'active' OR v_gate.revision<>v_attempt.gate_revision THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'output_id',p_output,'prefix',v_output.storage_ref ->> 'prefix',
        'storage_ref',v_output.storage_ref,'state',v_output.state,
        'job_id',v_attempt.job_id,'job_generation',v_attempt.job_generation,
        'lease_token',v_attempt.lease_token,'lease_expires_at',v_generation.lease_expires_at,
        'asset_id',v_attempt.asset_id,'generation',v_attempt.generation,
        'source_generation',v_attempt.source_generation,'source_snapshot',v_attempt.source_snapshot,
        'profile_version',v_attempt.profile_version,'gate_revision',v_attempt.gate_revision
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_attempt(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.allocate_native_pyramid_output(
    p_job uuid,p_job_generation bigint,p_lease uuid,p_asset uuid,p_generation integer
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_fixture aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
    v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
    v_binding public.storage_bindings%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_fixture_job boolean := false;
    v_output_id uuid := pg_catalog.gen_random_uuid();
    v_endpoint text;
    v_bucket text;
    v_key_prefix text;
    v_location_id uuid;
    v_config_revision integer;
    v_binding_generation bigint;
    v_storage_identity text;
    v_prefix text;
    v_ref jsonb;
    v_gate record;
BEGIN
    IF p_job IS NULL OR p_job_generation IS NULL OR p_job_generation<1
       OR p_lease IS NULL OR p_asset IS NULL OR p_generation IS NULL OR p_generation<1 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.asset_image_jobs WHERE job_id=p_job FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.kind<>'pyramid' OR v_job.status<>'running'
       OR v_job.generation<>p_job_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_job.pyramid_asset_id IS DISTINCT FROM p_asset
       OR v_job.pyramid_generation IS DISTINCT FROM p_generation THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    SELECT * INTO v_asset FROM public.image_pyramid_assets WHERE id=p_asset FOR UPDATE NOWAIT;
    SELECT * INTO v_generation FROM public.image_pyramid_generations
    WHERE asset_id=p_asset AND generation=p_generation FOR UPDATE NOWAIT;
    IF v_asset.id IS NULL OR v_generation.asset_id IS NULL
       OR v_asset.building_generation IS DISTINCT FROM p_generation
       OR v_generation.status<>'building'
       OR v_generation.job_id IS DISTINCT FROM p_job
       OR v_generation.job_generation IS DISTINCT FROM p_job_generation
       OR v_generation.lease_token IS DISTINCT FROM p_lease
       OR v_generation.lease_expires_at IS NULL
       OR v_generation.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_generation.source_generation IS DISTINCT FROM v_job.source_generation
       OR v_generation.source_snapshot IS DISTINCT FROM v_job.source_snapshot
       OR v_asset.profile_version IS DISTINCT FROM (v_job.spec #>> '{policy,profile_version}') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    -- An earlier attempt on another lease keeps its own saved output untouched.
    IF v_generation.output_id IS NOT NULL THEN
        SELECT * INTO v_output FROM public.image_pyramid_outputs
        WHERE id=v_generation.output_id FOR UPDATE NOWAIT;
        IF FOUND AND v_output.state='building' AND v_output.lease_token=p_lease
           AND v_output.asset_id=p_asset AND v_output.generation=p_generation THEN
            RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
                'output_id',v_output.id,'prefix',v_output.storage_ref ->> 'prefix',
                'job_id',p_job,'job_generation',p_job_generation,'lease_token',p_lease,
                'asset_id',p_asset,'generation',p_generation
            ), NULL);
        END IF;
    END IF;
    SELECT * INTO v_fixture FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family='image' AND job_id=p_job;
    v_fixture_job := FOUND;
    IF v_fixture_job THEN
        IF v_fixture.job_kind<>'pyramid' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
            WHERE claim.family='image' AND claim.job_id=p_job
              AND claim.job_generation=p_job_generation AND claim.lease_token=p_lease
              AND claim.state='claimed'
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
        IF (v_fixture.owner_kind='task' AND (v_asset.task_id IS DISTINCT FROM v_fixture.owner_id
              OR v_asset.dataset_item_id IS NOT NULL))
           OR (v_fixture.owner_kind<>'task' AND (v_asset.dataset_item_id IS DISTINCT FROM v_fixture.item_id
              OR v_asset.task_id IS NOT NULL)) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
        SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets
        WHERE run_id=v_fixture.run_id FOR SHARE;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
        v_endpoint := v_target.endpoint;
        v_bucket := v_target.bucket;
        v_storage_identity := v_target.storage_identity;
        v_location_id := v_target.location_id;
        v_binding_generation := v_target.binding_generation;
        v_key_prefix := '_aap_managed/media-cache/';
        SELECT location.config_revision INTO v_config_revision
        FROM public.storage_locations AS location WHERE location.id=v_location_id;
    ELSE
        SELECT * INTO v_binding FROM public.storage_bindings WHERE purpose='media-cache' FOR SHARE;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
        END IF;
        SELECT * INTO v_location FROM public.storage_locations WHERE id=v_binding.location_id FOR SHARE;
        IF NOT FOUND OR v_location.kind<>'s3' OR v_location.enabled IS NOT TRUE
           OR v_location.config_revision<1 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
        END IF;
        IF v_location.id='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7' THEN
            IF pg_catalog.jsonb_typeof(v_location.config -> 'use_ssl') IS DISTINCT FROM 'boolean'
               OR COALESCE(v_location.config ->> 'endpoint','') = '' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
            END IF;
            v_endpoint := CASE WHEN (v_location.config ->> 'use_ssl')::boolean
                THEN 'https://' ELSE 'http://' END || (v_location.config ->> 'endpoint');
            v_key_prefix := '';
        ELSE
            SELECT * INTO v_connection FROM public.storage_connections
            WHERE id=v_location.connection_id;
            IF NOT FOUND OR v_connection.kind<>'s3' OR v_connection.scope<>'global'
               OR v_connection.project_id IS NOT NULL OR v_connection.revision<1
               OR v_connection.secret_enc IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
            END IF;
            v_endpoint := v_connection.config ->> 'endpoint';
            v_key_prefix := pg_catalog.rtrim(COALESCE(v_connection.config ->> 'base_prefix',''),'/');
            IF v_key_prefix<>'' THEN
                IF v_key_prefix ~ '(^|/)[.][.]?(/|$)|//' THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
                END IF;
                v_key_prefix := v_key_prefix || '/';
            END IF;
            v_key_prefix := v_key_prefix || '_aap_managed/media-cache/';
        END IF;
        v_bucket := v_binding.bucket;
        v_location_id := v_location.id;
        v_config_revision := v_location.config_revision;
        v_binding_generation := v_binding.generation;
        v_storage_identity := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            pg_catalog.jsonb_build_array(
                pg_catalog.rtrim(v_endpoint,'/'),v_bucket
            )::text,'UTF8')),'hex');
    END IF;
    IF v_endpoint IS NULL OR v_endpoint !~ '^https?://[^[:space:]/?#@]+$'
       OR position(chr(92) in v_endpoint)>0
       OR v_bucket IS NULL OR pg_catalog.length(v_bucket) NOT BETWEEN 1 AND 255
       OR v_bucket ~ '[[:space:][:cntrl:]/?#@%:]' OR position(chr(92) in v_bucket)>0
       OR v_storage_identity IS NULL OR v_storage_identity !~ '^[0-9a-f]{64}$'
       OR v_location_id IS NULL OR v_config_revision IS NULL OR v_config_revision<1
       OR v_binding_generation IS NULL OR v_binding_generation<1
       OR v_key_prefix IS NULL OR pg_catalog.octet_length(v_key_prefix)>436 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
    END IF;
    INSERT INTO public.storage_maintenance_gates(location_id,state,revision)
    VALUES (v_location_id,'active',1) ON CONFLICT DO NOTHING;
    SELECT gate.state,gate.revision INTO v_gate FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id=v_location_id FOR SHARE;
    IF NOT FOUND OR v_gate.state<>'active' OR v_gate.revision<1 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
    END IF;
    v_prefix := v_key_prefix || 'image-pyramids/objects/' || v_output_id::text || '/';
    v_ref := pg_catalog.jsonb_build_object(
        'schema',2,'purpose','media-cache','location_id',v_location_id::text,
        'config_revision',v_config_revision,'binding_generation',v_binding_generation,
        'endpoint_url',pg_catalog.rtrim(v_endpoint,'/'),'bucket',v_bucket,
        'key_prefix',v_key_prefix,'prefix',v_prefix,'storage_identity',v_storage_identity
    );
    INSERT INTO public.image_pyramid_outputs(
        id,asset_id,generation,lease_token,storage_ref,state
    ) VALUES (v_output_id,p_asset,p_generation,p_lease,v_ref,'building');
    INSERT INTO aap_assets_v1.image_pyramid_native_attempts(
        output_id,job_id,job_generation,lease_token,asset_id,generation,
        source_generation,source_snapshot,profile_version,gate_revision
    ) VALUES (
        v_output_id,p_job,p_job_generation,p_lease,p_asset,p_generation,
        v_job.source_generation,v_job.source_snapshot,
        v_asset.profile_version,v_gate.revision
    );
    UPDATE public.image_pyramid_generations
    SET output_id=v_output_id,output_prefix=v_prefix,updated_at=pg_catalog.clock_timestamp()
    WHERE asset_id=p_asset AND generation=p_generation;
    -- The saved output owns its bytes; legacy prefix garbage must not remove them.
    UPDATE public.asset_image_jobs SET output_prefix=NULL WHERE job_id=p_job;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_output_id,'prefix',v_prefix,
        'job_id',p_job,'job_generation',p_job_generation,'lease_token',p_lease,
        'asset_id',p_asset,'generation',p_generation
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.allocate_native_pyramid_output(uuid,bigint,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_native_pyramid_member(
    p_operation uuid,p_output uuid,p_relative_key text,p_sha256 text,p_size bigint
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_attempt jsonb;
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_permit uuid;
    v_existing aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_purpose text;
BEGIN
    IF p_operation IS NULL OR p_output IS NULL OR p_relative_key IS NULL
       OR p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$'
       OR p_size IS NULL OR p_size<=0 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_attempt := aap_assets_v1._native_pyramid_attempt(p_output,true);
    IF v_attempt IS NULL OR v_attempt ->> 'state'<>'building' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    SELECT * INTO v_existing FROM aap_assets_v1.image_pyramid_native_members
    WHERE output_id=p_output AND relative_key=p_relative_key FOR UPDATE;
    IF FOUND THEN
        IF v_existing.operation_id IS DISTINCT FROM p_operation
           OR v_existing.sha256 IS DISTINCT FROM p_sha256
           OR v_existing.size_bytes IS DISTINCT FROM p_size THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',p_output,'operation_id',p_operation,'relative_key',p_relative_key,
            'sha256',p_sha256,'size_bytes',p_size,'state',v_existing.state
        ), NULL);
    END IF;
    IF EXISTS (SELECT 1 FROM aap_assets_v1.image_pyramid_native_members WHERE operation_id=p_operation) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_purpose := 'image_pyramid_' || pg_catalog.replace(p_output::text,'-','');
    v_permit := pg_catalog.gen_random_uuid();
    INSERT INTO public.storage_operations(id,location_id,revision,purpose,node_id)
    VALUES (
        v_permit,(v_attempt -> 'storage_ref' ->> 'location_id')::uuid,
        (v_attempt ->> 'gate_revision')::integer,v_purpose,
        'native-pyramid:' || (p_operation::text)
    );
    INSERT INTO aap_assets_v1.image_pyramid_native_members(
        operation_id,output_id,relative_key,sha256,size_bytes,state,operation_permit
    ) VALUES (p_operation,p_output,p_relative_key,p_sha256,p_size,'prepared',v_permit);
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state',v_member.state
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_native_pyramid_member(uuid,uuid,text,text,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.submit_native_pyramid_member(p_operation uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_attempt jsonb;
BEGIN
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state<>'prepared' THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    v_attempt := aap_assets_v1._native_pyramid_attempt(v_member.output_id,true);
    IF v_attempt IS NULL OR v_attempt ->> 'state'<>'building' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state='submitted',updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state='prepared';
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state','submitted'
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.submit_native_pyramid_member(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._native_pyramid_fixture_scope(p_output uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_attempt aap_assets_v1.image_pyramid_native_attempts%ROWTYPE;
    v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
    v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
    v_ref jsonb;
BEGIN
    SELECT * INTO v_attempt FROM aap_assets_v1.image_pyramid_native_attempts WHERE output_id=p_output;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family='image' AND job_id=v_attempt.job_id;
    IF NOT FOUND OR v_birth.job_kind<>'pyramid' OR v_birth.dataset_id IS NULL OR v_birth.item_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
        WHERE claim.family='image' AND claim.job_id=v_attempt.job_id
          AND claim.job_generation=v_attempt.job_generation
          AND claim.lease_token=v_attempt.lease_token AND claim.state='claimed'
    ) THEN RETURN NULL; END IF;
    SELECT storage_ref INTO v_ref FROM public.image_pyramid_outputs WHERE id=p_output;
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets
    WHERE run_id=v_birth.run_id;
    IF NOT FOUND OR v_ref->>'location_id' IS DISTINCT FROM v_target.location_id::text
       OR v_ref->>'endpoint_url' IS DISTINCT FROM v_target.endpoint
       OR v_ref->>'bucket' IS DISTINCT FROM v_target.bucket
       OR v_ref->>'storage_identity' IS DISTINCT FROM v_target.storage_identity THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'run_id',v_birth.run_id,'dataset_id',v_birth.dataset_id,'item_id',v_birth.item_id,
        'task_id',CASE WHEN v_birth.owner_kind='task' THEN v_birth.owner_id ELSE NULL END,
        'job_id',v_attempt.job_id,'lease_token',v_attempt.lease_token
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_fixture_scope(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.write_native_pyramid_member(
    p_operation uuid,p_etag text,p_version text
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_scope jsonb;
    v_key_digest bytea;
BEGIN
    IF p_etag IS NULL OR p_etag='' OR pg_catalog.length(p_etag)>1024
       OR (p_version IS NOT NULL AND (p_version='' OR p_version='null' OR pg_catalog.length(p_version)>1024)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state='written' THEN
        IF v_member.etag IS DISTINCT FROM p_etag OR v_member.version_id IS DISTINCT FROM p_version THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state<>'submitted' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state='written',etag=p_etag,version_id=p_version,updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state='submitted';
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_scope := aap_assets_v1._native_pyramid_fixture_scope(v_member.output_id);
    IF v_scope IS NOT NULL THEN
        v_key_digest := pg_catalog.sha256(pg_catalog.convert_to(
            (SELECT prefix FROM public.image_pyramid_outputs WHERE id=v_member.output_id)
            || v_member.relative_key,'UTF8'));
        INSERT INTO aap_assets_v1.fixture_media_versions(
            id,run_id,dataset_id,item_id,task_id,execution_kind,execution_id,execution_token,
            owner_kind,owner_id,object_key,key_digest,sha256,size_bytes,state,etag,version_id
        ) VALUES (
            p_operation,(v_scope ->> 'run_id')::uuid,(v_scope ->> 'dataset_id')::uuid,
            (v_scope ->> 'item_id')::uuid,(v_scope ->> 'task_id')::uuid,'pyramid',
            (v_scope ->> 'job_id')::uuid,(v_scope ->> 'lease_token')::uuid,
            'pyramid_output',v_member.output_id,
            (SELECT prefix FROM public.image_pyramid_outputs WHERE id=v_member.output_id)
                || v_member.relative_key,
            v_key_digest,v_member.sha256,v_member.size_bytes,'written',p_etag,p_version
        );
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state','written'
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.write_native_pyramid_member(uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.verify_native_pyramid_member(p_operation uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
BEGIN
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state='verified' THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state<>'written' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state='verified',updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state='written';
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    UPDATE aap_assets_v1.fixture_media_versions SET state='verified'
    WHERE id=p_operation AND state='written';
    DELETE FROM public.storage_operations WHERE id=v_member.operation_permit;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state','verified'
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.verify_native_pyramid_member(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.abandon_native_pyramid_member(p_operation uuid,p_state text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
BEGIN
    IF p_state NOT IN ('not_written','unknown') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state IN ('written','verified') THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state IN ('not_written','unknown') THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state=p_state,updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state IN ('prepared','submitted');
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF p_state='not_written' THEN
        DELETE FROM public.storage_operations WHERE id=v_member.operation_permit;
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state',p_state
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.abandon_native_pyramid_member(uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_native_pyramid_output(
    p_job_id uuid, p_generation bigint, p_lease_token uuid, p_result jsonb
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_image_jobs%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_attempt jsonb;
    v_spec jsonb;
    v_policy jsonb;
    v_prefix text;
    v_manifest jsonb;
    v_manifest_key text;
    v_overview_key text;
    v_manifest_sha text;
    v_manifest_size bigint;
    v_overview_sha text;
    v_overview_size bigint;
    v_width integer;
    v_height integer;
    v_overview_width integer;
    v_overview_height integer;
    v_tile_count bigint;
    v_retained_bytes bigint;
    v_expected_tiles bigint := 0;
    v_actual_tiles bigint;
    v_distinct_tiles bigint;
    v_members bigint;
    v_verified bigint;
    v_tile jsonb;
    v_level_object jsonb;
    v_level integer;
    v_x integer;
    v_y integer;
    v_columns integer;
    v_rows integer;
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_inventory jsonb;
    v_total bigint := 0;
    v_old_active integer;
    v_member_keys text[] := ARRAY[]::text[];
    v_tile_keys text[] := ARRAY[]::text[];
    v_key text;
    v_updated bigint;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation<1 OR p_lease_token IS NULL
       OR pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_result::text,'UTF8'))>8388608
       OR p_result ->> 'kind' IS DISTINCT FROM 'pyramid'
       OR NOT aap_assets_v1._media_object_identity_valid(p_result -> 'source_identity') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF NOT aap_assets_v1._media_object_has_only(
        p_result, ARRAY[
            'kind','source_identity','source_sha256','manifest','manifest_artifact',
            'overview_artifact','tile_artifacts','tile_count','retained_bytes'
        ]
    ) OR NOT (p_result ?& ARRAY[
            'kind','source_identity','source_sha256','manifest','manifest_artifact',
            'overview_artifact','tile_artifacts','tile_count','retained_bytes'
        ])
       OR pg_catalog.jsonb_typeof(p_result -> 'source_sha256') IS DISTINCT FROM 'string'
       OR p_result ->> 'source_sha256' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_result -> 'manifest') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_result -> 'tile_artifacts') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_result -> 'tile_artifacts')>20000
       OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'tile_count',0,9223372036854775807)
       OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'retained_bytes',0,9223372036854775807)
       OR NOT aap_assets_v1._media_artifact_valid(p_result -> 'manifest_artifact',p_result #>> '{manifest_artifact,key}')
       OR NOT aap_assets_v1._media_artifact_valid(p_result -> 'overview_artifact',p_result #>> '{overview_artifact,key}')
       OR (p_result #>> '{manifest_artifact,byte_size}')::numeric<=0
       OR (p_result #>> '{overview_artifact,byte_size}')::numeric<=0 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_candidate FROM public.asset_image_jobs AS job WHERE job.job_id=p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id,p_generation,'lease_lost');
    END IF;
    IF v_candidate.generation=p_generation AND v_candidate.last_lease_token=p_lease_token
       AND v_candidate.last_outcome IS NOT NULL THEN
        RETURN aap_assets_v1._image_exec_outcome(
            p_job_id,p_generation,
            CASE WHEN v_candidate.last_outcome IN ('source_changed','dependency_changed')
                 THEN v_candidate.last_outcome ELSE 'replayed' END
        );
    END IF;
    IF v_candidate.status<>'running' OR v_candidate.generation<>p_generation
       OR v_candidate.lease_token IS DISTINCT FROM p_lease_token
       OR v_candidate.kind<>'pyramid'
       OR v_candidate.pyramid_asset_id IS NULL OR v_candidate.pyramid_generation IS NULL THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id,p_generation,'lease_lost');
    END IF;
    IF NOT aap_assets_v1._image_exec_identity_equal(
            p_result -> 'source_identity', v_candidate.source_facts -> 'object_identity'
       ) OR (v_candidate.source_facts ->> 'content_hash' ~ '^[0-9a-f]{64}$'
             AND (v_candidate.source_facts ->> 'content_hash')
                 IS DISTINCT FROM (p_result ->> 'source_sha256')) THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id,p_generation,'source_changed');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_candidate.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_candidate.spec
       OR v_spec ->> 'kind' IS DISTINCT FROM 'pyramid' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_policy := v_spec -> 'policy';
    SELECT generation.* INTO v_generation
    FROM public.image_pyramid_generations AS generation
    WHERE generation.asset_id=v_candidate.pyramid_asset_id
      AND generation.generation=v_candidate.pyramid_generation
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_generation.job_id IS DISTINCT FROM p_job_id
       OR v_generation.job_generation IS DISTINCT FROM p_generation
       OR v_generation.lease_token IS DISTINCT FROM p_lease_token
       OR v_generation.lease_expires_at IS NULL
       OR v_generation.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_generation.status<>'building' OR v_generation.output_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    SELECT asset.* INTO v_asset FROM public.image_pyramid_assets AS asset
    WHERE asset.id=v_candidate.pyramid_asset_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_asset.building_generation IS DISTINCT FROM v_generation.generation THEN
        RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
    END IF;
    v_attempt := aap_assets_v1._native_pyramid_attempt(v_generation.output_id,true);
    IF v_attempt IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    SELECT * INTO v_output FROM public.image_pyramid_outputs
    WHERE id=v_generation.output_id FOR UPDATE NOWAIT;
    v_prefix := v_output.storage_ref ->> 'prefix';
    v_manifest := p_result -> 'manifest';
    IF v_prefix IS NULL OR pg_catalog.length(v_prefix)<=0 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF (p_result #>> '{manifest_artifact,key}') IS DISTINCT FROM v_prefix || 'manifest.json'
       OR (p_result #>> '{overview_artifact,key}') IS DISTINCT FROM v_prefix || 'overview.webp'
       OR 'sha256:' || (p_result #>> '{overview_artifact,sha256}')
          IS DISTINCT FROM v_manifest #>> '{overview,contentDigest}' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF NOT aap_assets_v1._image_exec_manifest_valid(
        v_manifest,v_generation.generation,p_result ->> 'source_sha256',v_policy
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_width := (v_manifest ->> 'width')::integer;
    v_height := (v_manifest ->> 'height')::integer;
    v_overview_width := (v_manifest #>> '{overview,width}')::integer;
    v_overview_height := (v_manifest #>> '{overview,height}')::integer;
    SELECT COALESCE(pg_catalog.sum(
        (level.value ->> 'columns')::bigint * (level.value ->> 'rows')::bigint
    ),0) INTO v_expected_tiles
    FROM pg_catalog.jsonb_array_elements(v_manifest -> 'levels') AS level(value);
    v_actual_tiles := pg_catalog.jsonb_array_length(p_result -> 'tile_artifacts');
    IF (p_result ->> 'tile_count')::bigint<>v_expected_tiles OR v_actual_tiles<>v_expected_tiles THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_manifest_key := v_prefix || 'manifest.json';
    v_overview_key := v_prefix || 'overview.webp';
    v_manifest_sha := p_result #>> '{manifest_artifact,sha256}';
    v_manifest_size := (p_result #>> '{manifest_artifact,byte_size}')::bigint;
    v_overview_sha := p_result #>> '{overview_artifact,sha256}';
    v_overview_size := (p_result #>> '{overview_artifact,byte_size}')::bigint;
    -- The manifest bytes are already canonical on the worker; publication only
    -- trusts a member row whose receipt proves exactly those bytes.
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE output_id=v_output.id AND relative_key='manifest.json';
    IF NOT FOUND OR v_member.state<>'verified' OR v_member.sha256 IS DISTINCT FROM v_manifest_sha
       OR v_member.size_bytes IS DISTINCT FROM v_manifest_size THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE output_id=v_output.id AND relative_key='overview.webp';
    IF NOT FOUND OR v_member.state<>'verified' OR v_member.sha256 IS DISTINCT FROM v_overview_sha
       OR v_member.size_bytes IS DISTINCT FROM v_overview_size THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_member_keys := ARRAY['manifest.json','overview.webp'];
    v_total := v_manifest_size + v_overview_size;
    FOR v_tile IN
        SELECT tile.value FROM pg_catalog.jsonb_array_elements(p_result -> 'tile_artifacts') AS tile(value)
    LOOP
        IF NOT aap_assets_v1._media_object_has_only(v_tile,ARRAY['level','x','y','artifact'])
           OR NOT (v_tile ?& ARRAY['level','x','y','artifact'])
           OR NOT aap_assets_v1._image_exec_integer_valid(v_tile -> 'level',0,2147483647)
           OR NOT aap_assets_v1._image_exec_integer_valid(v_tile -> 'x',0,2147483647)
           OR NOT aap_assets_v1._image_exec_integer_valid(v_tile -> 'y',0,2147483647) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_level := (v_tile ->> 'level')::integer;
        v_x := (v_tile ->> 'x')::integer;
        v_y := (v_tile ->> 'y')::integer;
        v_level_object := v_manifest #> ARRAY['levels',v_level::text];
        IF v_level_object IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_columns := (v_level_object ->> 'columns')::integer;
        v_rows := (v_level_object ->> 'rows')::integer;
        IF v_x>=v_columns OR v_y>=v_rows THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_key := 'tiles/' || v_level::text || '/' || v_x::text || '/' || v_y::text || '.webp';
        IF (v_tile #>> '{artifact,key}') IS DISTINCT FROM v_prefix || v_key
           OR (v_tile #>> '{artifact,byte_size}')::numeric<=0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
        WHERE output_id=v_output.id AND relative_key=v_key;
        IF NOT FOUND OR v_member.state<>'verified'
           OR v_member.sha256 IS DISTINCT FROM v_tile #>> '{artifact,sha256}'
           OR v_member.size_bytes IS DISTINCT FROM (v_tile #>> '{artifact,byte_size}')::bigint THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_member_keys := v_member_keys || v_key;
        v_tile_keys := v_tile_keys || v_key;
        v_total := v_total + v_member.size_bytes;
    END LOOP;
    SELECT pg_catalog.count(DISTINCT (tile.value ->> 'level') || ':' || (tile.value ->> 'x')
        || ':' || (tile.value ->> 'y'))
    INTO v_distinct_tiles
    FROM pg_catalog.jsonb_array_elements(p_result -> 'tile_artifacts') AS tile(value);
    IF v_distinct_tiles<>v_expected_tiles THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT pg_catalog.count(*),pg_catalog.count(*) FILTER (WHERE state='verified')
    INTO v_members,v_verified
    FROM aap_assets_v1.image_pyramid_native_members WHERE output_id=v_output.id;
    IF v_members<>pg_catalog.cardinality(v_member_keys) OR v_verified<>v_members THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF v_total<>(p_result ->> 'retained_bytes')::bigint
       OR v_total>(v_policy ->> 'max_derived_bytes')::numeric THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'schema',1,
        'objects',pg_catalog.jsonb_object_agg(
            v_prefix || member.relative_key,
            pg_catalog.jsonb_build_object('sha256',member.sha256,'size_bytes',member.size_bytes)
        )
    ) INTO v_inventory
    FROM aap_assets_v1.image_pyramid_native_members AS member
    WHERE member.output_id=v_output.id;
    IF v_inventory IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_old_active := v_asset.active_generation;
    IF v_candidate.owner_kind = 'dataset_item' THEN
        UPDATE public.dataset_items AS item
        SET width = COALESCE(item.width, v_width),
            height = COALESCE(item.height, v_height),
            media_materialized_generation = v_candidate.source_generation,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = v_candidate.owner_id
          AND item.media_source_generation = v_candidate.source_generation
          AND NOT item.media_source_drifted;
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        IF v_updated <> 1 THEN
            RETURN aap_assets_v1._image_exec_outcome(p_job_id,p_generation,'source_changed');
        END IF;
    END IF;
    UPDATE public.image_pyramid_outputs
    SET inventory=v_inventory,state='ready',updated_at=pg_catalog.clock_timestamp()
    WHERE id=v_output.id AND state='building';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
    END IF;
    UPDATE public.image_pyramid_generations
    SET status='ready',
        source_identity=aap_assets_v1._image_exec_legacy_identity(p_result -> 'source_identity'),
        source_fingerprint='sha256:' || (p_result ->> 'source_sha256'),
        source_generation=v_generation.source_generation,
        source_snapshot=v_generation.source_snapshot,
        output_prefix=v_prefix,
        manifest=v_manifest,policy=v_policy,
        lease_token=NULL,lease_expires_at=NULL,
        width=v_width,height=v_height,tile_size=512,overlap=1,
        max_level=pg_catalog.jsonb_array_length(v_manifest -> 'levels') - 1,
        format='webp',normalization_version=v_policy ->> 'normalization_version',
        manifest_key=v_manifest_key,
        manifest_digest='sha256:' || v_manifest_sha,
        overview_key=v_overview_key,
        overview_width=v_overview_width,overview_height=v_overview_height,
        overview_digest=v_manifest #>> '{overview,contentDigest}',
        tile_count=v_expected_tiles,retained_bytes=v_total,
        error_code=NULL,updated_at=pg_catalog.clock_timestamp()
    WHERE asset_id=v_generation.asset_id AND generation=v_generation.generation;
    UPDATE public.image_pyramid_assets
    SET active_generation=v_generation.generation,building_generation=NULL,
        updated_at=pg_catalog.clock_timestamp()
    WHERE id=v_asset.id;
    DELETE FROM public.storage_operations AS operation
    WHERE operation.purpose='image_pyramid_' || pg_catalog.replace(v_output.id::text,'-','');
    UPDATE public.asset_image_jobs
    SET status='ready',result=p_result,error_code=NULL,error_detail=NULL,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
        last_lease_token=p_lease_token,last_outcome='published',
        completed_at=pg_catalog.clock_timestamp(),updated_at=pg_catalog.clock_timestamp()
    WHERE job_id=p_job_id;
    RETURN aap_assets_v1._image_exec_outcome(p_job_id,p_generation,'published');
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_native_pyramid_output(uuid,bigint,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- A native pyramid output may be born only from its own leased attempt; the
-- ordinary target-based admission below still covers legacy fixture producers.
CREATE OR REPLACE FUNCTION aap_assets_v1.record_fixture_media_birth()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_kind text;
    v_id uuid;
    v_generation integer := 0;
    v_item uuid;
    v_task uuid;
    v_dataset uuid;
    v_run uuid;
    v_root uuid;
    v_state text;
    v_claimed boolean;
    v_target_run uuid;
    v_screenshot_admission uuid;
    v_root_state text;
    v_native jsonb;
BEGIN
    IF TG_TABLE_NAME = 'thumbnail_outputs' THEN
        IF NEW.owner_kind = 'dataset_thumbnail' THEN
            v_item := NEW.owner_id;
        ELSIF NEW.owner_kind = 'task_thumbnail' THEN
            v_task := NEW.owner_id;
        ELSE
            RETURN NEW;
        END IF;
        v_kind := 'thumbnail';
        v_id := NEW.id;
    ELSIF TG_TABLE_NAME = 'image_pyramid_assets' THEN
        v_item := NEW.dataset_item_id;
        v_task := NEW.task_id;
        v_kind := 'pyramid_asset';
        v_id := NEW.id;
    ELSIF TG_TABLE_NAME IN ('image_pyramid_generations', 'image_pyramid_outputs') THEN
        SELECT asset.dataset_item_id, asset.task_id INTO v_item, v_task
        FROM public.image_pyramid_assets AS asset WHERE asset.id = NEW.asset_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_missing';
        END IF;
        IF TG_TABLE_NAME = 'image_pyramid_generations' THEN
            v_kind := 'pyramid_generation';
            v_id := NEW.asset_id;
            v_generation := NEW.generation;
        ELSE
            v_kind := 'pyramid_output';
            v_id := NEW.id;
        END IF;
    ELSIF TG_TABLE_NAME = 'video_derivative_outputs' THEN
        v_item := NEW.dataset_item_id;
        v_kind := 'video_output';
        v_id := NEW.id;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_invalid';
    END IF;
    IF v_kind = 'pyramid_output' THEN
        v_native := aap_assets_v1._native_pyramid_fixture_scope(NEW.id);
        IF v_native IS NOT NULL THEN
            IF v_native ->> 'item_id' IS DISTINCT FROM v_item::text
               OR v_native ->> 'task_id' IS DISTINCT FROM v_task::text THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_birth_untracked';
            END IF;
            v_dataset := (v_native ->> 'dataset_id')::uuid;
            v_run := (v_native ->> 'run_id')::uuid;
            SELECT COALESCE(run.parent_run_id, run.id) INTO v_root
            FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run;
            SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
            WHERE root.id = v_root AND root.parent_run_id IS NULL FOR SHARE;
            SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.id = v_run AND COALESCE(run.parent_run_id, run.id) = v_root FOR SHARE;
            IF v_state NOT IN ('open','sealing') OR v_root_state NOT IN ('open','sealing')
               OR NOT EXISTS (
                   SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
                   JOIN yoao_fixture_v1.fixture_run_resources AS item ON item.run_id = dataset.run_id
                   WHERE dataset.run_id = v_run AND dataset.kind = 'dataset'
                     AND dataset.resource_id = v_dataset AND dataset.state = 'published'
                     AND ((v_item IS NOT NULL AND item.kind = 'item' AND item.resource_id = v_item
                           AND item.state = 'published')
                          OR (v_item IS NULL AND v_task IS NOT NULL
                              AND item.kind = 'task' AND item.resource_id = v_task
                              AND item.state = 'published'))
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_birth_closed';
            END IF;
            INSERT INTO aap_assets_v1.fixture_media_births
                (owner_kind, owner_id, generation, run_id, dataset_id, item_id, task_id)
            VALUES (v_kind, v_id, v_generation, v_run, v_dataset, v_item, v_task);
            RETURN NEW;
        END IF;
        IF v_task IS NOT NULL AND v_item IS NULL
           AND EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                       WHERE resource.kind = 'task' AND resource.resource_id = v_task) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_birth_untracked';
        END IF;
    END IF;
    IF v_task IS NOT NULL THEN
        SELECT COALESCE(task.dataset_item_id,(
            SELECT link.dataset_item_id FROM aap_labeling_v1.task_dataset_item_link_rows AS link
            WHERE link.task_id=task.id AND link.role='primary_lidar'
        )) INTO v_item FROM aap_labeling_v1.task_rows AS task WHERE task.id = v_task;
        IF NOT FOUND THEN
            IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                       WHERE resource.kind = 'task' AND resource.resource_id = v_task) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_missing';
            END IF;
            RETURN NEW;
        END IF;
    END IF;
    IF v_item IS NULL THEN
        IF v_task IS NULL THEN RETURN NEW; END IF;
        SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='task' AND resource.resource_id=v_task;
        IF NOT FOUND THEN RETURN NEW; END IF;
        IF v_kind IN ('thumbnail','pyramid_output','video_output') THEN
            SELECT target.run_id INTO v_target_run FROM aap_assets_v1.fixture_native_cache_targets AS target
            WHERE target.location_id::text=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,location_id}'
               OR target.storage_identity=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,storage_identity}';
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF v_target_run IS DISTINCT FROM v_run OR
               aap_assets_v1.fixture_legacy_birth_admission_current(v_run,NULL,NULL,v_task) IS DISTINCT FROM true THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_birth_untracked';
            END IF;
        ELSIF aap_assets_v1.fixture_legacy_birth_admission_current(v_run,NULL,NULL,v_task) IS DISTINCT FROM true THEN
            RETURN NEW;
        END IF;
        SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
        FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_run;
        SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.id=v_root AND root.parent_run_id IS NULL FOR SHARE;
        SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id=v_run AND COALESCE(run.parent_run_id,run.id)=v_root FOR SHARE;
        IF v_state NOT IN ('open','sealing') OR v_root_state NOT IN ('open','sealing')
           OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                          WHERE resource.run_id=v_run AND resource.kind='task'
                            AND resource.resource_id=v_task AND resource.state='published') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_birth_closed';
        END IF;
        INSERT INTO aap_assets_v1.fixture_media_births
            (owner_kind,owner_id,generation,run_id,task_id)
        VALUES (v_kind,v_id,v_generation,v_run,v_task);
        RETURN NEW;
    END IF;
    SELECT item.dataset_id INTO v_dataset FROM public.dataset_items AS item WHERE item.id = v_item;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS item
                   WHERE item.kind = 'item' AND item.resource_id = v_item) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_missing';
        END IF;
        RETURN NEW;
    END IF;
    SELECT dataset.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS dataset
    WHERE dataset.kind = 'dataset' AND dataset.resource_id = v_dataset;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF v_kind='video_output' AND pg_catalog.to_jsonb(NEW) ->> 'kind'='metadata' THEN
        v_screenshot_admission := aap_assets_v1.fixture_screenshot_producer_current(v_run,v_dataset,v_item);
    END IF;
    v_claimed := aap_assets_v1.fixture_legacy_birth_admission_current(v_run,v_dataset,v_item,NULL)
        OR v_screenshot_admission IS NOT NULL;
    IF v_kind IN ('pyramid_asset','pyramid_generation') THEN
        v_claimed := v_claimed OR aap_assets_v1.fixture_native_admission_current('image',v_run,v_dataset);
        IF NOT v_claimed THEN RETURN NEW; END IF;
    ELSE
        SELECT target.run_id INTO v_target_run FROM aap_assets_v1.fixture_native_cache_targets AS target
        WHERE target.location_id::text=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,location_id}'
           OR target.storage_identity=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,storage_identity}';
        IF NOT FOUND THEN
            IF v_claimed THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_target_untracked'; END IF;
            RETURN NEW;
        END IF;
        IF v_target_run IS DISTINCT FROM v_run OR NOT v_claimed THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_birth_untracked';
        END IF;
    END IF;
    SELECT COALESCE(run.parent_run_id, run.id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run;
    SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id = v_root AND root.parent_run_id IS NULL FOR SHARE;
    SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id = v_run AND COALESCE(run.parent_run_id, run.id) = v_root FOR SHARE;
    IF v_state NOT IN ('open','sealing') OR v_root_state NOT IN ('open','sealing')
       OR NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
           JOIN yoao_fixture_v1.fixture_run_resources AS item ON item.run_id = dataset.run_id
           WHERE dataset.run_id = v_run AND dataset.kind = 'dataset'
             AND dataset.resource_id = v_dataset AND dataset.state = 'published'
             AND item.kind = 'item' AND item.resource_id = v_item AND item.state = 'published'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_birth_closed';
    END IF;
    INSERT INTO aap_assets_v1.fixture_media_births
        (owner_kind, owner_id, generation, run_id, dataset_id, item_id, producer_admission_id)
    VALUES (v_kind, v_id, v_generation, v_run, v_dataset, v_item, v_screenshot_admission);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_fixture_media_birth() FROM PUBLIC;
-- aap-statement-break
DO $owner$
DECLARE
    v_owner name;
    v_create boolean;
    v_signature text;
    v_grant record;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT v_owner
    FROM pg_catalog.pg_proc
    WHERE oid='aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)'::regprocedure;
    v_create := pg_catalog.has_schema_privilege(v_owner,'aap_assets_v1','CREATE');
    IF NOT v_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_assets_v1 TO %I',v_owner);
    END IF;
    EXECUTE pg_catalog.format('ALTER TABLE aap_assets_v1.image_pyramid_native_attempts OWNER TO %I',v_owner);
    EXECUTE pg_catalog.format('ALTER TABLE aap_assets_v1.image_pyramid_native_members OWNER TO %I',v_owner);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_assets_v1.record_fixture_media_birth() OWNER TO %I',v_owner);
    -- The birth trigger's definer must be able to call its exact scope helper.
    FOR v_grant IN
        SELECT pg_catalog.pg_get_userbyid(proowner) AS rolname
        FROM pg_catalog.pg_proc
        WHERE oid='aap_assets_v1.record_fixture_media_birth()'::regprocedure
        UNION
        SELECT v_owner::text
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION aap_assets_v1._native_pyramid_fixture_scope(uuid) TO %I',
            v_grant.rolname
        );
    END LOOP;
    IF NOT v_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_assets_v1 FROM %I',v_owner);
    END IF;
    FOR v_grant IN
        SELECT role.rolname,acl.is_grantable
        FROM pg_catalog.pg_proc AS procedure
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
        JOIN pg_catalog.pg_roles AS role ON role.oid=acl.grantee
        WHERE procedure.oid='aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)'::regprocedure
          AND acl.privilege_type='EXECUTE' AND acl.grantee<>procedure.proowner
    LOOP
        FOREACH v_signature IN ARRAY ARRAY[
            'prepare_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb)',
            'retry_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb)'
        ] LOOP
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION aap_assets_v1.%s TO %I%s',
                v_signature,v_grant.rolname,
                CASE WHEN v_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
            );
        END LOOP;
    END LOOP;
    FOR v_grant IN
        SELECT role.rolname,acl.is_grantable
        FROM pg_catalog.pg_proc AS procedure
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
        JOIN pg_catalog.pg_roles AS role ON role.oid=acl.grantee
        WHERE procedure.oid='aap_assets_v1.claim_image_media(uuid,uuid,integer,text[])'::regprocedure
          AND acl.privilege_type='EXECUTE' AND acl.grantee<>procedure.proowner
    LOOP
        FOREACH v_signature IN ARRAY ARRAY[
            'allocate_native_pyramid_output(uuid,bigint,uuid,uuid,integer)',
            'begin_native_pyramid_member(uuid,uuid,text,text,bigint)',
            'submit_native_pyramid_member(uuid)',
            'write_native_pyramid_member(uuid,text,text)',
            'verify_native_pyramid_member(uuid)',
            'abandon_native_pyramid_member(uuid,text)',
            'finish_native_pyramid_output(uuid,bigint,uuid,jsonb)'
        ] LOOP
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION aap_assets_v1.%s TO %I%s',
                v_signature,v_grant.rolname,
                CASE WHEN v_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
            );
        END LOOP;
    END LOOP;
    -- Owner-only plumbing used by the functions above.
    FOREACH v_signature IN ARRAY ARRAY[
        '_accept_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb,text)',
        '_native_pyramid_attempt(uuid,boolean)',
        '_native_pyramid_fixture_scope(uuid)',
        'guard_image_pyramid_native_attempt()',
        'guard_image_pyramid_native_member()'
    ] LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION aap_assets_v1.%s OWNER TO %I',v_signature,v_owner);
    END LOOP;
    FOR v_grant IN
        SELECT role.rolname
        FROM pg_catalog.pg_class AS relation
        CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS acl
        JOIN pg_catalog.pg_roles AS role ON role.oid=acl.grantee
        WHERE relation.oid='public.image_pyramid_outputs'::regclass
          AND acl.privilege_type IN ('SELECT','INSERT','UPDATE')
          AND acl.grantee<>relation.relowner
        GROUP BY role.rolname
        HAVING pg_catalog.count(DISTINCT acl.privilege_type)=3
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT SELECT,INSERT,UPDATE ON aap_assets_v1.image_pyramid_native_attempts,aap_assets_v1.image_pyramid_native_members TO %I',
            v_grant.rolname
        );
    END LOOP;
END;
$owner$;
