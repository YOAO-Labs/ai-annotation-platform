-- Assets-owned upload intents.  The caller retains the transaction boundary;
-- no function signs URLs, copies objects, publishes messages, or commits.
CREATE TABLE public.dataset_upload_intents (
    item_id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES public.datasets(id) ON DELETE CASCADE,
    actor_id uuid NOT NULL,
    file_name varchar(500) NOT NULL,
    file_path varchar(1000) NULL,
    file_type varchar(20) NOT NULL CHECK (file_type IN ('image', 'video', 'other')),
    content_type varchar(255) NOT NULL,
    staging_key varchar(128) NOT NULL UNIQUE,
    publication_id uuid NULL,
    state varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'completed', 'discarded')),
    result jsonb NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT ck_dataset_upload_intents_publication_pair CHECK (
        (publication_id IS NULL) = (file_path IS NULL)
    ),
    CONSTRAINT ck_dataset_upload_intents_file_path_length CHECK (
        file_path IS NULL OR pg_catalog.char_length(file_path) <= 1000
    ),
    CONSTRAINT ck_dataset_upload_intents_result CHECK (
        CASE state
            WHEN 'pending' THEN result IS NULL
            WHEN 'completed' THEN
                result IS NOT NULL
                AND publication_id IS NOT NULL
                AND pg_catalog.jsonb_typeof(result) = 'object'
                AND result ?& ARRAY['item_id', 'linked_tasks']
                AND (result - ARRAY['item_id', 'linked_tasks']) = '{}'::jsonb
                AND pg_catalog.jsonb_typeof(result -> 'item_id') = 'string'
                AND result ->> 'item_id' IS NOT NULL
                AND result ->> 'item_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                AND pg_catalog.jsonb_typeof(result -> 'linked_tasks') = 'number'
                AND result ->> 'linked_tasks' IS NOT NULL
                AND result ->> 'linked_tasks' ~ '^(0|[1-9][0-9]*)$'
            WHEN 'discarded' THEN
                result IS NOT NULL
                AND pg_catalog.jsonb_typeof(result) = 'object'
                AND (
                    (
                        result ? 'duplicate_of'
                        AND NOT (result ? 'reason')
                        AND (result - ARRAY['duplicate_of']) = '{}'::jsonb
                        AND pg_catalog.jsonb_typeof(result -> 'duplicate_of') = 'string'
                        AND result ->> 'duplicate_of' IS NOT NULL
                        AND result ->> 'duplicate_of' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        AND result ->> 'duplicate_of' <> item_id::text
                    )
                    OR (
                        result ? 'reason'
                        AND NOT (result ? 'duplicate_of')
                        AND (result - ARRAY['reason']) = '{}'::jsonb
                        AND pg_catalog.jsonb_typeof(result -> 'reason') = 'string'
                        AND result ->> 'reason' IN ('missing', 'source_deleted')
                    )
                )
            ELSE false
        END
    )
);
-- aap-statement-break
REVOKE ALL ON TABLE public.dataset_upload_intents FROM PUBLIC;
-- aap-statement-break
CREATE INDEX ix_dataset_upload_intents_dataset_state
ON public.dataset_upload_intents (dataset_id, state, item_id);
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_upload_failure(p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-upload/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_upload_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_upload_success(p_data jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-upload/v1', 'ok', true, 'data', p_data
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_upload_success(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_upload_actor(p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_code text;
    v_role text;
BEGIN
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'upload identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_actor #>> '{error,code}';
        IF v_code IS NULL OR v_code NOT IN ('account_not_active', 'account_busy') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'upload identity failure mismatch';
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure(v_code);
    END IF;
    IF pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR (v_actor -> 'data' - ARRAY['actor_id', 'platform_role']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,actor_id}') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'upload identity facts mismatch';
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_account_role');
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        pg_catalog.jsonb_build_object('actor_id', p_actor_id)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_upload_actor(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_upload_facts(
    p_intent public.dataset_upload_intents
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'item_id', (p_intent).item_id,
        'actor_id', (p_intent).actor_id,
        'dataset_id', (p_intent).dataset_id,
        'file_name', (p_intent).file_name,
        'file_path', (p_intent).file_path,
        'file_type', (p_intent).file_type,
        'content_type', (p_intent).content_type,
        'staging_key', (p_intent).staging_key,
        'publication_id', (p_intent).publication_id,
        'state', (p_intent).state,
        'result', (p_intent).result
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_upload_facts(public.dataset_upload_intents) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accept_dataset_upload(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid,
    p_publication_id uuid,
    p_file_size bigint,
    p_content_hash text,
    p_width integer,
    p_height integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_duplicate_id uuid;
    v_expected_path text;
    v_updated bigint;
BEGIN
    SELECT intent.* INTO v_intent
    FROM public.dataset_upload_intents AS intent
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id
      AND intent.actor_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.state <> 'pending' THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    IF v_intent.publication_id IS NULL
       OR v_intent.publication_id IS DISTINCT FROM p_publication_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    v_expected_path := 'dataset-sources/' || p_dataset_id::text || '/'
        || p_item_id::text || '/' || p_publication_id::text || '/'
        || v_intent.file_name;
    IF v_intent.file_path IS DISTINCT FROM v_expected_path THEN
        RETURN aap_assets_v1._dataset_upload_failure('source_changed');
    END IF;

    IF p_content_hash IS NOT NULL THEN
        SELECT item.id INTO v_duplicate_id
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
          AND item.id <> p_item_id
          AND item.content_hash IS NOT NULL
          AND item.content_hash <> ''
          AND pg_catalog.lower(item.content_hash) = p_content_hash
        ORDER BY item.id
        LIMIT 1;
        IF v_duplicate_id IS NOT NULL THEN
            UPDATE public.dataset_upload_intents AS intent
            SET state = 'discarded',
                result = pg_catalog.jsonb_build_object('duplicate_of', v_duplicate_id),
                updated_at = pg_catalog.now()
            WHERE intent.item_id = p_item_id
              AND intent.dataset_id = p_dataset_id
              AND intent.actor_id = p_actor_id
              AND intent.state = 'pending'
            RETURNING intent.* INTO v_intent;
            RETURN aap_assets_v1._dataset_upload_success(
                pg_catalog.jsonb_build_object(
                    'disposition', 'duplicate', 'duplicate_of', v_duplicate_id
                )
            );
        END IF;
    END IF;

    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_item_id;
    IF FOUND THEN
        IF v_item.dataset_id IS DISTINCT FROM p_dataset_id
           OR v_item.file_name IS DISTINCT FROM v_intent.file_name
           OR v_item.file_path IS DISTINCT FROM v_intent.file_path
           OR v_item.file_type IS DISTINCT FROM v_intent.file_type THEN
            RETURN aap_assets_v1._dataset_upload_failure('source_changed');
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
    END IF;

    BEGIN
        INSERT INTO public.dataset_items (
            id, dataset_id, file_name, file_path, file_type, file_size,
            content_hash, width, height, metadata
        )
        VALUES (
            p_item_id, p_dataset_id, v_intent.file_name, v_intent.file_path,
            v_intent.file_type, p_file_size, p_content_hash, p_width, p_height,
            '{}'::jsonb
        );
    EXCEPTION
        WHEN unique_violation THEN
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
    END;

    UPDATE public.datasets AS dataset
    SET file_count = COALESCE(dataset.file_count, 0) + 1,
        updated_at = pg_catalog.now()
    WHERE dataset.id = p_dataset_id;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'upload dataset disappeared';
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        pg_catalog.jsonb_build_object('disposition', 'accepted')
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accept_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._finish_dataset_upload(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid,
    p_publication_id uuid,
    p_linked_tasks bigint
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
BEGIN
    SELECT intent.* INTO v_intent
    FROM public.dataset_upload_intents AS intent
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id
      AND intent.actor_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.state <> 'pending'
       OR v_intent.publication_id IS DISTINCT FROM p_publication_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_item_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_item.dataset_id IS DISTINCT FROM p_dataset_id
       OR v_item.file_name IS DISTINCT FROM v_intent.file_name
       OR v_item.file_path IS DISTINCT FROM v_intent.file_path
       OR v_item.file_type IS DISTINCT FROM v_intent.file_type THEN
        RETURN aap_assets_v1._dataset_upload_failure('source_changed');
    END IF;
    UPDATE public.dataset_upload_intents AS intent
    SET state = 'completed',
        result = pg_catalog.jsonb_build_object(
            'item_id', p_item_id, 'linked_tasks', p_linked_tasks
        ),
        updated_at = pg_catalog.now()
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id
      AND intent.actor_id = p_actor_id
      AND intent.publication_id = p_publication_id
      AND intent.state = 'pending'
    RETURNING intent.* INTO v_intent;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        aap_assets_v1._dataset_upload_facts(v_intent)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._finish_dataset_upload(uuid, uuid, uuid, uuid, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.reserve_dataset_upload(
    p_item_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_file_name text,
    p_content_type text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_dataset_name text;
    v_storage_root text;
    v_file_type text;
    v_staging_key text;
    v_intent public.dataset_upload_intents%ROWTYPE;
BEGIN
    IF p_item_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_file_name IS NULL OR pg_catalog.char_length(p_file_name) NOT BETWEEN 1 AND 500
       OR p_file_name ~ '^/' OR p_file_name ~ '^[A-Za-z]:/'
       OR p_file_name ~ '[[:cntrl:]]'
       OR pg_catalog.strpos(p_file_name, pg_catalog.chr(92)) > 0
       OR p_file_name ~ '(^|/)([.][.]?)(/|$)'
       OR p_file_name ~ '//'
       OR pg_catalog.right(p_file_name, 1) = '/'
       OR p_content_type IS NULL
       OR pg_catalog.char_length(p_content_type) NOT BETWEEN 1 AND 255
       OR p_content_type <> pg_catalog.btrim(p_content_type)
       OR p_content_type ~ '[[:cntrl:]]' THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    BEGIN
        SELECT dataset.name INTO v_dataset_name
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_missing');
    END IF;
    SELECT intent.* INTO v_intent
    FROM public.dataset_upload_intents AS intent
    WHERE intent.item_id = p_item_id;
    IF FOUND THEN
        IF (v_intent.actor_id, v_intent.dataset_id, v_intent.file_name,
            v_intent.content_type) IS DISTINCT FROM
           (p_actor_id, p_dataset_id, p_file_name, p_content_type) THEN
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
        END IF;
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
          AND intent.dataset_id = p_dataset_id
          AND intent.actor_id = p_actor_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
        END IF;
        IF (v_intent.file_name, v_intent.content_type) IS DISTINCT FROM
           (p_file_name, p_content_type) THEN
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
        END IF;
        IF v_intent.state <> 'pending' THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
        END IF;
        v_storage_root := pg_catalog.split_part(
            pg_catalog.btrim(v_dataset_name, '/'), '/', 1
        );
        IF pg_catalog.left(
            v_storage_root, pg_catalog.char_length('__aap_trusted_nuscenes__')
        ) = '__aap_trusted_nuscenes__' THEN
            RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
        END IF;
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
        );
    END IF;
    v_storage_root := pg_catalog.split_part(
        pg_catalog.btrim(v_dataset_name, '/'), '/', 1
    );
    IF pg_catalog.left(
        v_storage_root, pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.dataset_items AS item WHERE item.id = p_item_id
    ) THEN
        RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
    END IF;

    v_file_type := CASE
        WHEN p_content_type LIKE 'image/%' THEN 'image'
        WHEN p_content_type LIKE 'video/%' THEN 'video'
        ELSE 'other'
    END;
    v_staging_key := 'dataset-uploads/' || p_item_id::text || '/source';
    INSERT INTO public.dataset_upload_intents (
        item_id, dataset_id, actor_id, file_name, file_type,
        content_type, staging_key
    )
    VALUES (
        p_item_id, p_dataset_id, p_actor_id, p_file_name, v_file_type,
        p_content_type, v_staging_key
    )
    ON CONFLICT (item_id) DO NOTHING
    RETURNING * INTO v_intent;

    IF NOT FOUND THEN
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
        END IF;
        IF (v_intent.actor_id, v_intent.dataset_id, v_intent.file_name,
            v_intent.content_type) IS DISTINCT FROM
           (p_actor_id, p_dataset_id, p_file_name, p_content_type) THEN
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
        END IF;
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
          AND intent.dataset_id = p_dataset_id
          AND intent.actor_id = p_actor_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
        END IF;
        IF (v_intent.file_name, v_intent.content_type) IS DISTINCT FROM
           (p_file_name, p_content_type) THEN
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
        END IF;
        IF v_intent.state <> 'pending' THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
        END IF;
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        aap_assets_v1._dataset_upload_facts(v_intent)
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_dataset_upload(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_dataset_name text;
    v_storage_root text;
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_publication_id uuid;
    v_file_path text;
    v_expected_path text;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_item_id IS NULL THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    BEGIN
        SELECT dataset.name INTO v_dataset_name
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_missing');
    END IF;
    BEGIN
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
          AND intent.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        IF EXISTS (
            SELECT 1 FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = p_item_id
        ) THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    IF v_intent.publication_id IS NOT NULL THEN
        v_expected_path := 'dataset-sources/' || p_dataset_id::text || '/'
            || p_item_id::text || '/' || v_intent.publication_id::text || '/'
            || v_intent.file_name;
        IF v_intent.file_path IS DISTINCT FROM v_expected_path THEN
            RETURN aap_assets_v1._dataset_upload_failure('source_changed');
        END IF;
    END IF;
    IF v_intent.state <> 'pending' THEN
        IF v_intent.state = 'completed' THEN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = p_item_id;
            IF FOUND AND (
                v_item.dataset_id IS DISTINCT FROM p_dataset_id
                OR v_item.file_name IS DISTINCT FROM v_intent.file_name
                OR v_item.file_path IS DISTINCT FROM v_intent.file_path
                OR v_item.file_type IS DISTINCT FROM v_intent.file_type
            ) THEN
                RETURN aap_assets_v1._dataset_upload_failure('source_changed');
            END IF;
        END IF;
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
        );
    END IF;
    v_storage_root := pg_catalog.split_part(
        pg_catalog.btrim(v_dataset_name, '/'), '/', 1
    );
    IF pg_catalog.left(
        v_storage_root, pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;

    IF v_intent.publication_id IS NOT NULL THEN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_item_id;
        IF FOUND THEN
            IF v_item.dataset_id IS DISTINCT FROM p_dataset_id
               OR v_item.file_name IS DISTINCT FROM v_intent.file_name
               OR v_item.file_path IS DISTINCT FROM v_intent.file_path
               OR v_item.file_type IS DISTINCT FROM v_intent.file_type THEN
                RETURN aap_assets_v1._dataset_upload_failure('source_changed');
            END IF;
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
        END IF;
    ELSIF EXISTS (
        SELECT 1 FROM public.dataset_items AS item WHERE item.id = p_item_id
    ) THEN
        RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
    END IF;

    v_publication_id := pg_catalog.gen_random_uuid();
    v_file_path := 'dataset-sources/' || p_dataset_id::text || '/'
        || p_item_id::text || '/' || v_publication_id::text || '/'
        || v_intent.file_name;
    IF pg_catalog.char_length(v_file_path) > 1000 THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    UPDATE public.dataset_upload_intents AS intent
    SET publication_id = v_publication_id,
        file_path = v_file_path,
        updated_at = pg_catalog.now()
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id
      AND intent.actor_id = p_actor_id
      AND intent.state = 'pending'
    RETURNING intent.* INTO v_intent;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        aap_assets_v1._dataset_upload_facts(v_intent)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.find_dataset_upload_duplicate(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid,
    p_content_hash text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_duplicate_id uuid;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_item_id IS NULL
       OR (p_content_hash IS NOT NULL AND
           p_content_hash !~ '^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$') THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    SELECT intent.* INTO v_intent
    FROM public.dataset_upload_intents AS intent
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id;
    IF NOT FOUND THEN
        IF EXISTS (
            SELECT 1 FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = p_item_id
        ) THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    IF v_intent.state <> 'pending' OR v_intent.publication_id IS NULL THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    IF p_content_hash IS NOT NULL THEN
        SELECT item.id INTO v_duplicate_id
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
          AND item.id <> p_item_id
          AND item.content_hash IS NOT NULL
          AND item.content_hash <> ''
          AND pg_catalog.lower(item.content_hash) = pg_catalog.lower(p_content_hash)
        ORDER BY item.id
        LIMIT 1;
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        pg_catalog.jsonb_build_object('duplicate_of', v_duplicate_id)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_dataset_upload(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid,
    p_publication_id uuid,
    p_file_size bigint,
    p_content_hash text,
    p_width integer,
    p_height integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_dataset_name text;
    v_storage_root text;
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_accept jsonb;
    v_append jsonb;
    v_result jsonb;
    v_disposition text;
    v_duplicate_id uuid;
    v_linked_tasks bigint;
    v_error_detail text;
    v_error_code text;
    v_expected_path text;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_item_id IS NULL
       OR p_publication_id IS NULL OR p_file_size IS NULL OR p_file_size < 0
       OR (p_content_hash IS NOT NULL AND
           p_content_hash !~ '^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$')
       OR ((p_width IS NULL) <> (p_height IS NULL))
       OR p_width <= 0 OR p_height <= 0 THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    IF p_content_hash IS NOT NULL THEN
        p_content_hash := pg_catalog.lower(p_content_hash);
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    BEGIN
        SELECT dataset.name INTO v_dataset_name
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_missing');
    END IF;
    BEGIN
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
          AND intent.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        IF EXISTS (
            SELECT 1 FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = p_item_id
        ) THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    IF v_intent.publication_id IS DISTINCT FROM p_publication_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    v_expected_path := 'dataset-sources/' || p_dataset_id::text || '/'
        || p_item_id::text || '/' || v_intent.publication_id::text || '/'
        || v_intent.file_name;
    IF v_intent.file_path IS DISTINCT FROM v_expected_path THEN
        RETURN aap_assets_v1._dataset_upload_failure('source_changed');
    END IF;
    IF v_intent.state = 'completed' THEN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_item_id;
        IF FOUND AND (
            v_item.dataset_id IS DISTINCT FROM p_dataset_id
            OR v_item.file_name IS DISTINCT FROM v_intent.file_name
            OR v_item.file_path IS DISTINCT FROM v_intent.file_path
            OR v_item.file_type IS DISTINCT FROM v_intent.file_type
        ) THEN
            RETURN aap_assets_v1._dataset_upload_failure('source_changed');
        END IF;
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
            || pg_catalog.jsonb_build_object('disposition', 'accepted')
        );
    END IF;
    IF v_intent.state = 'discarded' THEN
        IF v_intent.result ? 'duplicate_of' THEN
            RETURN aap_assets_v1._dataset_upload_success(
                aap_assets_v1._dataset_upload_facts(v_intent)
                || pg_catalog.jsonb_build_object(
                    'disposition', 'duplicate',
                    'duplicate_of', v_intent.result -> 'duplicate_of'
                )
            );
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    v_storage_root := pg_catalog.split_part(
        pg_catalog.btrim(v_dataset_name, '/'), '/', 1
    );
    IF pg_catalog.left(
        v_storage_root, pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;

    v_accept := aap_assets_v1._accept_dataset_upload(
        p_actor_id, p_dataset_id, p_item_id, p_publication_id,
        p_file_size, p_content_hash, p_width, p_height
    );
    IF v_accept -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_accept;
    END IF;
    v_disposition := v_accept #>> '{data,disposition}';
    IF v_disposition = 'duplicate' THEN
        v_duplicate_id := (v_accept #>> '{data,duplicate_of}')::uuid;
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id;
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
            || pg_catalog.jsonb_build_object(
                'disposition', 'duplicate', 'duplicate_of', v_duplicate_id
            )
        );
    END IF;
    IF v_disposition IS DISTINCT FROM 'accepted' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'upload accept contract mismatch';
    END IF;

    v_append := aap_labeling_v1.append_dataset_items(
        p_dataset_id, ARRAY[p_item_id]::uuid[], p_item_id
    );
    IF pg_catalog.jsonb_typeof(v_append) IS DISTINCT FROM 'object'
       OR (v_append - ARRAY['schema', 'operation_id', 'dataset_id', 'created',
                            'project_totals', 'associations']) <> '{}'::jsonb
       OR NOT (v_append ?& ARRAY['schema', 'operation_id', 'dataset_id', 'created',
                                 'project_totals', 'associations'])
       OR v_append ->> 'schema' IS DISTINCT FROM 'aap-task-append/v1'
       OR v_append ->> 'operation_id' IS DISTINCT FROM p_item_id::text
       OR v_append ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text
       OR pg_catalog.jsonb_typeof(v_append -> 'created') IS DISTINCT FROM 'number'
       OR v_append ->> 'created' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_append -> 'project_totals') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_append -> 'associations') IS DISTINCT FROM 'array'
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_append -> 'project_totals') AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
               OR (entry.value - ARRAY['project_id', 'total_tasks']) <> '{}'::jsonb
               OR NOT (entry.value ?& ARRAY['project_id', 'total_tasks'])
               OR entry.value ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR entry.value ->> 'total_tasks' !~ '^(0|[1-9][0-9]*)$'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_append -> 'associations') AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
               OR (entry.value - ARRAY['id', 'project_id']) <> '{}'::jsonb
               OR NOT (entry.value ?& ARRAY['id', 'project_id'])
               OR entry.value ->> 'id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR entry.value ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'task append contract mismatch';
    END IF;
    v_linked_tasks := (v_append ->> 'created')::bigint;
    v_result := aap_assets_v1._finish_dataset_upload(
        p_actor_id, p_dataset_id, p_item_id, p_publication_id, v_linked_tasks
    );
    IF v_result -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error_code := v_result #>> '{error,code}';
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'upload finish contract failed',
            DETAIL = pg_catalog.jsonb_build_object('code', v_error_code)::text;
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        (v_result -> 'data')
        || pg_catalog.jsonb_build_object('disposition', 'accepted')
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    WHEN SQLSTATE '22023' THEN
        GET STACKED DIAGNOSTICS v_error_detail = PG_EXCEPTION_DETAIL;
        IF v_error_detail IS NOT NULL THEN
            BEGIN
                v_error_code := v_error_detail::jsonb ->> 'code';
            EXCEPTION WHEN others THEN
                v_error_code := NULL;
            END;
        END IF;
        IF v_error_code IN ('operation_conflict', 'source_changed', 'upload_not_pending') THEN
            RETURN aap_assets_v1._dataset_upload_failure(v_error_code);
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.discard_dataset_upload(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid,
    p_reason text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_dataset_id uuid;
    v_intent public.dataset_upload_intents%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_item_id IS NULL
       OR p_reason IS DISTINCT FROM 'missing' THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    BEGIN
        SELECT dataset.id INTO v_dataset_id
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_missing');
    END IF;
    BEGIN
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
          AND intent.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        IF EXISTS (
            SELECT 1 FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = p_item_id
        ) THEN
            RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
        END IF;
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    IF v_intent.state = 'discarded' AND v_intent.result = pg_catalog.jsonb_build_object(
        'reason', 'missing'
    ) THEN
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
        );
    END IF;
    IF v_intent.state <> 'pending' THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    UPDATE public.dataset_upload_intents AS intent
    SET state = 'discarded',
        result = pg_catalog.jsonb_build_object('reason', 'missing'),
        updated_at = pg_catalog.now()
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id
      AND intent.actor_id = p_actor_id
      AND intent.state = 'pending'
    RETURNING intent.* INTO v_intent;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        aap_assets_v1._dataset_upload_facts(v_intent)
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text) FROM PUBLIC;
