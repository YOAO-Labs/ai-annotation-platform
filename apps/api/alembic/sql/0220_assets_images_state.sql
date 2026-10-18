-- Accepted image work owns execution. Pyramid rows are delivery projections.
DO $block$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.image_pyramid_generations
        WHERE status IN ('pending', 'building')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Drain accepted legacy image pyramid writers before migration';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.dataset_import_deliveries
        WHERE kind = 'thumbnail' AND delivered_at IS NULL
    ) OR EXISTS (
        SELECT 1 FROM public.dataset_source_ingest_deliveries
        WHERE kind = 'thumbnail' AND delivered_at IS NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Drain legacy thumbnail deliveries before migration; their source identity cannot be reconstructed';
    END IF;
END;
$block$;
-- aap-statement-break
ALTER TABLE public.image_pyramid_assets
    DROP CONSTRAINT image_pyramid_assets_dataset_item_id_fkey,
    DROP CONSTRAINT image_pyramid_assets_task_id_fkey;
-- aap-statement-break
ALTER TABLE public.image_pyramid_generations
    ADD COLUMN job_id uuid,
    ADD COLUMN job_generation bigint CHECK (job_generation > 0),
    ADD COLUMN source_generation bigint CHECK (source_generation > 0),
    ADD COLUMN source_snapshot varchar(64)
        CHECK (source_snapshot ~ '^[0-9a-f]{64}$'),
    ADD COLUMN output_prefix varchar(1024),
    ADD COLUMN manifest jsonb,
    ADD COLUMN policy jsonb;
-- aap-statement-break
CREATE TABLE public.asset_image_jobs (
    job_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    kind varchar(32) NOT NULL,
    owner_kind varchar(16),
    owner_id uuid,
    slot_key varchar(255) NOT NULL CHECK (pg_catalog.char_length(slot_key) BETWEEN 1 AND 255),
    generation bigint NOT NULL DEFAULT 1,
    spec jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(spec) = 'object'),
    status varchar(20) NOT NULL DEFAULT 'pending',
    source_generation bigint NOT NULL DEFAULT 0,
    source_snapshot varchar(64) CHECK (source_snapshot ~ '^[0-9a-f]{64}$'),
    source_facts jsonb CHECK (pg_catalog.jsonb_typeof(source_facts) = 'object'),
    actor_id uuid,
    progress jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (pg_catalog.jsonb_typeof(progress) = 'object'),
    result jsonb CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    error_code varchar(64),
    error_detail text CHECK (pg_catalog.char_length(error_detail) <= 2000),
    attempt_count integer NOT NULL DEFAULT 0,
    lease_owner uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    output_prefix varchar(1024),
    pyramid_asset_id uuid,
    pyramid_generation integer CHECK (pyramid_generation > 0),
    last_lease_token uuid,
    last_outcome varchar(24) CHECK (last_outcome IN (
        'published', 'failed', 'lease_lost', 'source_changed', 'dependency_changed'
    )),
    accepted_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    completed_at timestamptz,
    backfill_cutoff timestamptz,
    backfill_cursor_kind varchar(16),
    backfill_cursor_id uuid,
    CONSTRAINT ck_asset_image_jobs_kind CHECK (
        kind IN ('dimensions', 'thumbnail', 'pyramid', 'image_backfill')
    ),
    CONSTRAINT ck_asset_image_jobs_status CHECK (
        status IN ('pending', 'running', 'ready', 'failed', 'canceled')
    ),
    CONSTRAINT ck_asset_image_jobs_generation CHECK (generation >= 1),
    CONSTRAINT ck_asset_image_jobs_source_generation CHECK (source_generation >= 0),
    CONSTRAINT ck_asset_image_jobs_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_asset_image_jobs_lease CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
        AND (lease_token IS NULL) = (lease_owner IS NULL)
    ),
    CONSTRAINT ck_asset_image_jobs_last_outcome CHECK (
        (last_lease_token IS NULL) = (last_outcome IS NULL)
    ),
    CONSTRAINT ck_asset_image_jobs_scope CHECK (
        (kind = 'image_backfill' AND owner_kind IS NULL AND owner_id IS NULL
         AND source_generation = 0 AND source_snapshot IS NULL
         AND source_facts IS NULL AND backfill_cutoff IS NOT NULL)
        OR
        (kind <> 'image_backfill' AND owner_kind IN ('dataset_item', 'task')
         AND owner_id IS NOT NULL AND source_generation > 0
         AND source_snapshot IS NOT NULL AND source_facts IS NOT NULL
         AND pg_catalog.jsonb_typeof(source_facts -> 'object_identity')
             IS NOT DISTINCT FROM 'object'
         AND backfill_cutoff IS NULL AND backfill_cursor_kind IS NULL
         AND backfill_cursor_id IS NULL)
    ),
    CONSTRAINT ck_asset_image_jobs_cursor CHECK (
        (backfill_cursor_kind IS NULL) = (backfill_cursor_id IS NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_asset_image_jobs_owner_slot
    ON public.asset_image_jobs(owner_kind, owner_id, slot_key)
    WHERE owner_id IS NOT NULL;
-- aap-statement-break
CREATE UNIQUE INDEX uq_asset_image_jobs_backfill_slot
    ON public.asset_image_jobs(slot_key) WHERE owner_id IS NULL;
-- aap-statement-break
CREATE INDEX ix_asset_image_jobs_claim
    ON public.asset_image_jobs(status, lease_expires_at, accepted_at);
-- aap-statement-break
CREATE INDEX ix_asset_image_jobs_owner_status
    ON public.asset_image_jobs(owner_kind, owner_id, status);
-- aap-statement-break
CREATE INDEX ix_asset_image_jobs_pyramid_status
    ON public.asset_image_jobs(pyramid_asset_id, pyramid_generation, status)
    WHERE pyramid_asset_id IS NOT NULL;
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_image_jobs FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_image_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid,
    action varchar(16) NOT NULL CHECK (action IN ('request', 'retry', 'cancel')),
    scope jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(scope) = 'object'),
    intent jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(intent) = 'object'),
    result jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
        AND result ->> 'schema' = 'aap-assets-images/v1'
        AND result -> 'ok' = 'true'::jsonb
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
CREATE INDEX ix_asset_image_operations_created_at ON public.asset_image_operations(created_at);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_image_operations FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_asset_image_operations_immutable
BEFORE UPDATE OR DELETE ON public.asset_image_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._media_operation_immutable();
-- aap-statement-break
CREATE TABLE public.asset_image_garbage (
    garbage_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    prefix varchar(1024) NOT NULL UNIQUE,
    reason varchar(64) NOT NULL CHECK (pg_catalog.char_length(reason) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    not_before timestamptz NOT NULL DEFAULT pg_catalog.now(),
    completed_at timestamptz,
    lease_owner uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    last_ack_token uuid,
    CONSTRAINT ck_asset_image_garbage_lease CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
        AND (lease_token IS NULL) = (lease_owner IS NULL)
    )
);
-- aap-statement-break
CREATE INDEX ix_asset_image_garbage_due
    ON public.asset_image_garbage(not_before, lease_expires_at, garbage_id);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_image_garbage FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-images/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-images/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_garbage_prefix_valid(p_prefix text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        p_prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/(g[1-9][0-9]*/(a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/)?)?$'
        OR p_prefix ~ '^image-assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$',
        false
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_garbage_prefix_valid(text) FROM PUBLIC;
-- aap-statement-break
ALTER TABLE public.asset_image_garbage ADD CONSTRAINT ck_asset_image_garbage_prefix
    CHECK (aap_assets_v1._image_garbage_prefix_valid(prefix));
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_garbage_immutable_scope()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'image garbage scopes are retained';
    END IF;
    IF (NEW.garbage_id, NEW.prefix, NEW.reason, NEW.created_at)
       IS DISTINCT FROM (OLD.garbage_id, OLD.prefix, OLD.reason, OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'image garbage scopes are immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_garbage_immutable_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_asset_image_garbage_immutable_scope
BEFORE UPDATE OR DELETE ON public.asset_image_garbage
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_garbage_immutable_scope();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._queue_image_garbage(
    p_prefix text, p_reason text,
    p_not_before timestamptz DEFAULT pg_catalog.now()
)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._image_garbage_prefix_valid(p_prefix)
       OR p_reason IS NULL OR pg_catalog.char_length(p_reason) NOT BETWEEN 1 AND 64
       OR p_not_before IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid image garbage scope';
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-image-garbage:' || p_prefix, 0)
    ) THEN
        RAISE lock_not_available USING MESSAGE = 'image garbage scope is busy';
    END IF;
    PERFORM garbage.garbage_id FROM public.asset_image_garbage AS garbage
    WHERE garbage.prefix = p_prefix FOR UPDATE NOWAIT;
    INSERT INTO public.asset_image_garbage(prefix, reason, not_before)
    VALUES (p_prefix, p_reason, p_not_before)
    ON CONFLICT (prefix) DO UPDATE
    SET not_before = GREATEST(public.asset_image_garbage.not_before, EXCLUDED.not_before);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._queue_image_garbage(text, text, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_work_reference(p_job public.asset_image_jobs)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id', (p_job).job_id, 'generation', (p_job).generation,
        'owner', CASE WHEN (p_job).owner_id IS NOT NULL
            THEN pg_catalog.jsonb_build_object('kind', (p_job).owner_kind, 'id', (p_job).owner_id) END,
        'source_generation', (p_job).source_generation,
        'kind', (p_job).kind, 'status', (p_job).status
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_work_reference(public.asset_image_jobs) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_read_operation(
    p_operation_id uuid, p_actor_id uuid, p_action text, p_scope jsonb, p_intent jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.asset_image_operations%ROWTYPE;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-assets-images:' || p_operation_id::text, 0)
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END IF;
    SELECT receipt.* INTO v_operation FROM public.asset_image_operations AS receipt
    WHERE receipt.operation_id = p_operation_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF (v_operation.actor_id, v_operation.action, v_operation.scope, v_operation.intent)
       IS DISTINCT FROM (p_actor_id, p_action, p_scope, p_intent) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
    END IF;
    RETURN v_operation.result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_read_operation(uuid, uuid, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_write_operation(
    p_operation_id uuid, p_actor_id uuid, p_action text,
    p_scope jsonb, p_intent jsonb, p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_operation_id IS NOT NULL THEN
        INSERT INTO public.asset_image_operations(operation_id, actor_id, action, scope, intent, result)
        VALUES (p_operation_id, p_actor_id, p_action, p_scope, p_intent, p_result);
    END IF;
    RETURN p_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_write_operation(uuid, uuid, text, jsonb, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_policy(p_policy jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb := '{"profile_version":"pyramid-v1","normalization_version":"exif-autorotate-srgb-v1","auto_generate_pyramids":false,"optional_pixels":16777216,"required_pixels":50000000,"max_pixels":300000000,"max_dimension":32768,"max_tiles":20000,"thumbnail_max_source_bytes":536870912,"max_source_bytes":8589934592,"max_derived_bytes":8589934592,"max_temp_bytes":12884901888,"job_timeout_seconds":1800,"lease_seconds":2100,"retry_cooldown_seconds":300,"gc_grace_hours":24,"url_expiry_seconds":900,"asset_url_batch_max":128,"vips_concurrency":4}'::jsonb;
    v_key text;
    v_bound numeric;
BEGIN
    IF pg_catalog.jsonb_typeof(p_policy) IS DISTINCT FROM 'object'
       OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_policy) AS field(name)
            WHERE NOT (v_policy ? field.name)
       ) THEN RETURN NULL; END IF;
    v_policy := v_policy || p_policy;
    IF pg_catalog.jsonb_typeof(v_policy -> 'profile_version') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(v_policy ->> 'profile_version') NOT BETWEEN 1 AND 64
       OR v_policy ->> 'normalization_version' IS DISTINCT FROM 'exif-autorotate-srgb-v1'
       OR pg_catalog.jsonb_typeof(v_policy -> 'auto_generate_pyramids') IS DISTINCT FROM 'boolean'
       OR v_policy -> 'thumbnail_max_source_bytes' IS DISTINCT FROM '536870912'::jsonb THEN
        RETURN NULL;
    END IF;
    FOR v_key IN SELECT field.name FROM pg_catalog.jsonb_object_keys(v_policy) AS field(name)
        WHERE field.name NOT IN ('profile_version', 'normalization_version', 'auto_generate_pyramids')
    LOOP
        IF pg_catalog.jsonb_typeof(v_policy -> v_key) IS DISTINCT FROM 'number'
           OR v_policy ->> v_key !~ '^(0|[1-9][0-9]*)$' THEN RETURN NULL; END IF;
        v_bound := CASE WHEN v_key IN (
            'optional_pixels', 'required_pixels', 'max_pixels',
            'max_source_bytes', 'max_derived_bytes', 'max_temp_bytes'
        ) THEN 9223372036854775807 ELSE 2147483647 END;
        IF (v_policy ->> v_key)::numeric > v_bound
           OR ((v_policy ->> v_key)::numeric = 0
               AND v_key NOT IN ('retry_cooldown_seconds', 'gc_grace_hours')) THEN
            RETURN NULL;
        END IF;
    END LOOP;
    IF (v_policy ->> 'optional_pixels')::numeric > (v_policy ->> 'required_pixels')::numeric
       OR (v_policy ->> 'required_pixels')::numeric > (v_policy ->> 'max_pixels')::numeric
       OR (v_policy ->> 'lease_seconds')::numeric
            <= (v_policy ->> 'job_timeout_seconds')::numeric + 60 THEN
        RETURN NULL;
    END IF;
    RETURN v_policy;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_policy(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_backfill_selection(p_selection jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_selection jsonb;
    v_id text;
    v_count integer;
BEGIN
    IF pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY[
            'owner_kind', 'dataset_id', 'project_id', 'owner_ids', 'all_owners',
            'purpose', 'only_missing', 'accepted_before'
       ]) <> '{}'::jsonb THEN RETURN NULL; END IF;
    v_selection := pg_catalog.jsonb_build_object(
        'dataset_id', NULL, 'project_id', NULL, 'owner_ids', '[]'::jsonb,
        'all_owners', false, 'only_missing', true, 'accepted_before', NULL
    ) || p_selection;
    IF v_selection ->> 'owner_kind' IS NULL
       OR v_selection ->> 'owner_kind' NOT IN ('dataset_item', 'task')
       OR v_selection ->> 'purpose' IS NULL
       OR v_selection ->> 'purpose' NOT IN ('dimensions', 'thumbnail', 'pyramid')
       OR pg_catalog.jsonb_typeof(v_selection -> 'all_owners') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_selection -> 'only_missing') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_selection -> 'owner_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_selection -> 'owner_ids') > 500 THEN
        RETURN NULL;
    END IF;
    FOREACH v_id IN ARRAY ARRAY['dataset_id', 'project_id'] LOOP
        IF v_selection -> v_id <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_selection -> v_id) IS DISTINCT FROM 'string'
               OR (v_selection ->> v_id)::uuid IS NULL THEN RETURN NULL; END IF;
        END IF;
    END LOOP;
    FOR v_id IN SELECT value #>> '{}' FROM pg_catalog.jsonb_array_elements(v_selection -> 'owner_ids') AS element(value)
    LOOP
        IF v_id IS NULL OR v_id::uuid IS NULL THEN RETURN NULL; END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_selection -> 'owner_ids') AS element(value)
        WHERE pg_catalog.jsonb_typeof(element.value) <> 'string'
    ) OR (
        SELECT pg_catalog.count(DISTINCT (value #>> '{}')::uuid)
        FROM pg_catalog.jsonb_array_elements(v_selection -> 'owner_ids') AS element(value)
    ) <> pg_catalog.jsonb_array_length(v_selection -> 'owner_ids') THEN RETURN NULL; END IF;
    v_count := (v_selection -> 'dataset_id' <> 'null'::jsonb)::integer
        + (v_selection -> 'project_id' <> 'null'::jsonb)::integer
        + (pg_catalog.jsonb_array_length(v_selection -> 'owner_ids') > 0)::integer
        + (v_selection ->> 'all_owners')::boolean::integer;
    IF v_count <> 1 OR (v_selection ->> 'owner_kind' = 'task'
        AND v_selection -> 'dataset_id' <> 'null'::jsonb) THEN RETURN NULL; END IF;
    IF v_selection -> 'accepted_before' <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(v_selection -> 'accepted_before') IS DISTINCT FROM 'string'
           OR v_selection ->> 'accepted_before' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR (v_selection ->> 'accepted_before')::timestamptz IS NULL THEN RETURN NULL; END IF;
    END IF;
    RETURN v_selection;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
    OR datetime_field_overflow OR invalid_datetime_format THEN RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_backfill_selection(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_spec(p_spec jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
    v_selection jsonb;
    v_kind text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_spec) IS DISTINCT FROM 'object' THEN RETURN NULL; END IF;
    v_kind := p_spec ->> 'kind';
    v_policy := aap_assets_v1._image_policy(COALESCE(p_spec -> 'policy', '{}'::jsonb));
    IF v_policy IS NULL OR v_kind IS NULL THEN RETURN NULL; END IF;
    IF v_kind = 'image_backfill' THEN
        IF (p_spec - ARRAY['kind', 'selection', 'policy']) <> '{}'::jsonb THEN RETURN NULL; END IF;
        v_selection := aap_assets_v1._image_backfill_selection(p_spec -> 'selection');
        IF v_selection IS NULL THEN RETURN NULL; END IF;
        RETURN pg_catalog.jsonb_build_object('kind', v_kind, 'selection', v_selection, 'policy', v_policy);
    END IF;
    IF v_kind NOT IN ('dimensions', 'thumbnail', 'pyramid')
       OR (p_spec - CASE WHEN v_kind = 'dimensions'
            THEN ARRAY['kind', 'expected_source_snapshot', 'policy', 'generate_preview']
            ELSE ARRAY['kind', 'expected_source_snapshot', 'policy'] END) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_spec -> 'expected_source_snapshot') IS DISTINCT FROM 'string'
       OR p_spec ->> 'expected_source_snapshot' !~ '^[0-9a-f]{64}$' THEN RETURN NULL; END IF;
    IF v_kind = 'dimensions' AND p_spec ? 'generate_preview'
       AND pg_catalog.jsonb_typeof(p_spec -> 'generate_preview') IS DISTINCT FROM 'boolean' THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'kind', v_kind, 'expected_source_snapshot', p_spec -> 'expected_source_snapshot',
        'policy', v_policy
    ) || CASE WHEN v_kind = 'dimensions' THEN pg_catalog.jsonb_build_object(
        'generate_preview', COALESCE(p_spec -> 'generate_preview', 'false'::jsonb)
    ) ELSE '{}'::jsonb END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_spec(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_next_generation(p_asset_id uuid)
RETURNS integer
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_max numeric;
BEGIN
    IF p_asset_id IS NULL THEN RETURN NULL; END IF;
    PERFORM asset.id FROM public.image_pyramid_assets AS asset
    WHERE asset.id = p_asset_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF EXISTS (
        SELECT 1 FROM public.asset_image_garbage AS garbage
        WHERE garbage.prefix = 'image-pyramids/' || p_asset_id::text || '/'
    ) THEN RETURN NULL; END IF;
    SELECT GREATEST(
        COALESCE((SELECT pg_catalog.max(generation.generation)
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = p_asset_id), 0),
        COALESCE((SELECT pg_catalog.max(job.pyramid_generation)
            FROM public.asset_image_jobs AS job
            WHERE job.pyramid_asset_id = p_asset_id), 0),
        COALESCE((SELECT pg_catalog.max(
                (pg_catalog.substring(garbage.prefix, '/g([1-9][0-9]*)/'))::numeric
            ) FROM public.asset_image_garbage AS garbage
            WHERE pg_catalog.left(garbage.prefix, 52) = 'image-pyramids/' || p_asset_id::text || '/'), 0)
    ) INTO v_max;
    IF v_max >= 2147483647 THEN RETURN NULL; END IF;
    RETURN v_max::integer + 1;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_next_generation(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._cancel_image_source_work(
    p_owner_kind text, p_owner_id uuid, p_reason text
)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_deadline timestamptz := pg_catalog.statement_timestamp();
    v_prefix text;
    v_grace integer;
BEGIN
    IF p_owner_kind IS NULL OR p_owner_kind NOT IN ('dataset_item', 'task')
       OR p_owner_id IS NULL OR p_reason IS NULL
       OR pg_catalog.char_length(p_reason) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid image cancellation scope';
    END IF;
    FOR v_job IN
        SELECT job.* FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = p_owner_kind AND job.owner_id = p_owner_id
        ORDER BY job.job_id FOR UPDATE NOWAIT
    LOOP
        v_deadline := GREATEST(v_deadline, COALESCE(v_job.lease_expires_at, v_deadline));
        IF v_job.output_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, p_reason,
                GREATEST(pg_catalog.statement_timestamp(), v_job.lease_expires_at)
            );
        END IF;
        IF v_job.status IN ('pending', 'running') THEN
            UPDATE public.asset_image_jobs AS job
            SET status = 'canceled', error_code = 'source_changed',
                error_detail = p_reason, completed_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp(),
                last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN v_job.lease_token ELSE job.last_lease_token END,
                last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN 'source_changed' ELSE job.last_outcome END,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job.job_id = v_job.job_id;
        END IF;
    END LOOP;
    FOR v_asset IN
        SELECT asset.* FROM public.image_pyramid_assets AS asset
        WHERE (p_owner_kind = 'dataset_item' AND asset.dataset_item_id = p_owner_id)
           OR (p_owner_kind = 'task' AND asset.task_id = p_owner_id)
        ORDER BY asset.id FOR UPDATE NOWAIT
    LOOP
        FOR v_generation IN
            SELECT generation.* FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
            ORDER BY generation.generation FOR UPDATE NOWAIT
        LOOP
            v_deadline := GREATEST(v_deadline, COALESCE(v_generation.lease_expires_at, v_deadline));
            v_prefix := COALESCE(v_generation.output_prefix,
                'image-pyramids/' || v_asset.id::text || '/g' || v_generation.generation::text || '/');
            v_grace := COALESCE((v_generation.policy ->> 'gc_grace_hours')::integer, 24);
            PERFORM aap_assets_v1._queue_image_garbage(v_prefix, p_reason,
                GREATEST(v_deadline,
                    pg_catalog.statement_timestamp() + pg_catalog.make_interval(hours => v_grace))
            );
            IF v_generation.status IN ('pending', 'building') THEN
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = 'source_changed',
                    updated_at = pg_catalog.clock_timestamp()
                WHERE generation.asset_id = v_asset.id
                  AND generation.generation = v_generation.generation;
            END IF;
        END LOOP;
        UPDATE public.image_pyramid_assets AS asset
        SET active_generation = NULL, building_generation = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE asset.id = v_asset.id;
    END LOOP;
    RETURN v_deadline;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._cancel_image_source_work(text, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.invalidate_task_image_source(p_task_id uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM aap_assets_v1._cancel_image_source_work('task', p_task_id, 'source_changed');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.invalidate_task_image_source(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_source_execution_invalidated()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM aap_assets_v1.invalidate_task_image_source(OLD.id);
        RETURN OLD;
    END IF;
    IF NEW.media_source_generation IS DISTINCT FROM OLD.media_source_generation THEN
        PERFORM aap_assets_v1.invalidate_task_image_source(OLD.id);
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_source_execution_invalidated() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_tasks_image_source_execution_invalidated
AFTER UPDATE ON public.tasks
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._image_source_execution_invalidated();
-- aap-statement-break
CREATE TRIGGER trg_tasks_image_source_execution_deleted
BEFORE DELETE ON public.tasks
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._image_source_execution_invalidated();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_dataset_binding_changed()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.dataset_id IS DISTINCT FROM OLD.dataset_id
       AND NEW.media_source_generation = OLD.media_source_generation THEN
        IF OLD.media_source_generation = 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'image source generation exhausted';
        END IF;
        NEW.media_source_generation := OLD.media_source_generation + 1;
        NEW.media_object_identity := NULL;
        NEW.media_source_drifted := false;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_dataset_binding_changed() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_dataset_items_z_image_binding_generation
BEFORE UPDATE OF dataset_id ON public.dataset_items
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_dataset_binding_changed();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_item_source_invalidated()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_prefix text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.file_type = 'image' THEN
            PERFORM aap_assets_v1._cancel_image_source_work('dataset_item', OLD.id, 'source_deleted');
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.media_source_generation IS NOT DISTINCT FROM OLD.media_source_generation
       OR (OLD.file_type IS DISTINCT FROM 'image' AND NEW.file_type IS DISTINCT FROM 'image') THEN
        RETURN NEW;
    END IF;
    PERFORM aap_assets_v1._cancel_image_source_work('dataset_item', OLD.id, 'source_changed');
    v_prefix := pg_catalog.regexp_replace(OLD.thumbnail_path, '(thumbnail|overview)\.webp$', '');
    IF aap_assets_v1._image_garbage_prefix_valid(v_prefix) THEN
        PERFORM aap_assets_v1._queue_image_garbage(v_prefix, 'source_changed');
    END IF;
    UPDATE public.dataset_items AS item
    SET thumbnail_path = NULL, blurhash = NULL, media_materialized_generation = 0,
        width = CASE WHEN NEW.width IS DISTINCT FROM OLD.width THEN NEW.width END,
        height = CASE WHEN NEW.height IS DISTINCT FROM OLD.height THEN NEW.height END,
        metadata = COALESCE(item.metadata, '{}'::jsonb) - 'thumbnail_error'
    WHERE item.id = NEW.id;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_item_source_invalidated() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_dataset_items_image_source_invalidated
AFTER UPDATE ON public.dataset_items
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_item_source_invalidated();
-- aap-statement-break
CREATE TRIGGER trg_dataset_items_image_source_deleted
BEFORE DELETE ON public.dataset_items
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_item_source_invalidated();
