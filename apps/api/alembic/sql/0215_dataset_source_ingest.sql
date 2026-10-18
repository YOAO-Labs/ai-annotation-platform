-- Assets-owned synchronous ZIP and legacy-prefix ingest state.
CREATE TABLE public.dataset_source_ingest_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('zip', 'scan')),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    descriptor jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(descriptor) = 'object'),
    state text NOT NULL CHECK (state IN ('preparing', 'completed')),
    creator_transaction_id bigint NOT NULL,
    dataset_name varchar(255) NOT NULL,
    is_temporal boolean NOT NULL,
    source_prefix text NOT NULL,
    next_index integer NOT NULL DEFAULT 0 CHECK (next_index >= 0),
    added integer NOT NULL DEFAULT 0 CHECK (added >= 0),
    deduped integer NOT NULL DEFAULT 0 CHECK (deduped >= 0),
    skipped integer NOT NULL DEFAULT 0 CHECK (skipped >= 0),
    error_count integer NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    linked_tasks integer NOT NULL DEFAULT 0 CHECK (linked_tasks >= 0),
    layout_snapshot_digest text,
    layout_scene_count integer,
    layout_assigned_count integer,
    layout_total_items integer,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    completed_at timestamptz,
    CONSTRAINT ck_dataset_source_ingest_operations_layout_pair CHECK (
        (layout_snapshot_digest IS NULL) = (layout_scene_count IS NULL)
        AND (layout_scene_count IS NULL) = (layout_assigned_count IS NULL)
        AND (layout_assigned_count IS NULL) = (layout_total_items IS NULL)
    ),
    CONSTRAINT ck_dataset_source_ingest_operations_layout_digest CHECK (
        layout_snapshot_digest IS NULL OR layout_snapshot_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_dataset_source_ingest_operations_layout_counts CHECK (
        layout_scene_count IS NULL OR (
            layout_scene_count >= 0 AND layout_assigned_count >= 0
            AND layout_total_items >= layout_assigned_count
        )
    ),
    CONSTRAINT ck_dataset_source_ingest_operations_result_state CHECK (
        (state = 'completed') = (result IS NOT NULL)
    ),
    CONSTRAINT ck_dataset_source_ingest_operations_result_scope CHECK (
        result IS NULL OR (
            pg_catalog.jsonb_typeof(result) = 'object'
            AND result ->> 'operation_id' = operation_id::text
            AND result ->> 'actor_id' = actor_id::text
            AND result ->> 'dataset_id' = dataset_id::text
        )
    )
);
-- aap-statement-break
REVOKE ALL ON public.dataset_source_ingest_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_source_ingest_operations IS
    'Assets-owned transaction-bound synchronous source ingest operations and retained receipts; no live resource foreign keys.';
-- aap-statement-break
CREATE INDEX ix_dataset_source_ingest_operations_dataset_state
    ON public.dataset_source_ingest_operations (dataset_id, state, created_at);
-- aap-statement-break
CREATE TABLE public.dataset_source_ingest_entries (
    operation_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    entry_index integer NOT NULL CHECK (entry_index >= 0),
    batch_id uuid NOT NULL,
    source jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(source) = 'object'),
    planned_status text NOT NULL CHECK (
        planned_status IN ('pending', 'deduped', 'skipped', 'error')
    ),
    status text NOT NULL CHECK (
        status IN ('pending', 'added', 'deduped', 'skipped', 'error')
    ),
    item_id uuid,
    publication_id uuid,
    file_name varchar(500),
    file_path varchar(1000),
    error_code text CHECK (
        error_code IS NULL OR error_code IN (
            'invalid_source_object', 'archive_entry_read_failed',
            'storage_write_failed', 'source_changed', 'source_missing',
            'source_read_failed'
        )
    ),
    completion jsonb CHECK (
        completion IS NULL OR pg_catalog.jsonb_typeof(completion) = 'object'
    ),
    file_size bigint CHECK (file_size IS NULL OR file_size >= 0),
    content_hash text CHECK (
        content_hash IS NULL OR content_hash ~ '^[0-9a-f]{32}$'
    ),
    width integer CHECK (width IS NULL OR width > 0),
    height integer CHECK (height IS NULL OR height > 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    settled_at timestamptz,
    PRIMARY KEY (operation_id, entry_id),
    CONSTRAINT ck_dataset_source_ingest_entries_dimension_pair CHECK (
        (width IS NULL) = (height IS NULL)
    ),
    CONSTRAINT ck_dataset_source_ingest_entries_pending_plan CHECK (
        status <> 'pending' OR (
            planned_status = 'pending' AND item_id IS NOT NULL
            AND file_name IS NOT NULL AND file_path IS NOT NULL
            AND settled_at IS NULL
        )
    ),
    CONSTRAINT ck_dataset_source_ingest_entries_error_settled CHECK (
        status <> 'error' OR (error_code IS NOT NULL AND settled_at IS NOT NULL)
    ),
    CONSTRAINT ck_dataset_source_ingest_entries_nonerror_settled CHECK (
        status NOT IN ('added', 'deduped', 'skipped') OR settled_at IS NOT NULL
    ),
    CONSTRAINT fk_dataset_source_ingest_entries_operation FOREIGN KEY (operation_id)
        REFERENCES public.dataset_source_ingest_operations (operation_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_dataset_source_ingest_entries_index UNIQUE (operation_id, entry_index),
    CONSTRAINT uq_dataset_source_ingest_entries_item UNIQUE (operation_id, item_id),
    CONSTRAINT uq_dataset_source_ingest_entries_publication UNIQUE (
        operation_id, publication_id
    )
);
-- aap-statement-break
REVOKE ALL ON public.dataset_source_ingest_entries FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_source_ingest_entries IS
    'Assets-owned scoped plans, storage proofs, and settlement state for source ingest entries.';
-- aap-statement-break
CREATE INDEX ix_dataset_source_ingest_entries_batch
    ON public.dataset_source_ingest_entries (operation_id, batch_id, entry_index);
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-source-ingest/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-ingest/v1',
        'ok', true,
        'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_integer_valid(
    p_value jsonb, p_min numeric, p_max numeric
)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_value IS NULL OR p_min IS NULL OR p_max IS NULL
             OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'number'
            THEN false
        WHEN p_value #>> '{}' !~ '^(0|[1-9][0-9]*)$' THEN false
        ELSE (p_value #>> '{}')::numeric BETWEEN p_min AND p_max
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_integer_valid(jsonb, numeric, numeric)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_descriptor_valid(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_kind text;
    v_total bigint;
    v_planned bigint;
    v_hidden bigint;
    v_unsafe bigint;
BEGIN
    IF p_value IS NULL OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF NOT (p_value ?& ARRAY[
            'kind', 'request_digest', 'total_in_zip', 'planned_entries'
       ])
       OR (p_value - ARRAY[
            'kind', 'request_digest', 'total_in_zip', 'planned_entries',
            'hidden_count', 'unsafe_count', 'top_level_dirs'
       ]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_value -> 'kind') <> 'string'
       OR pg_catalog.jsonb_typeof(p_value -> 'request_digest') <> 'string'
       OR p_value ->> 'request_digest' !~ '^[0-9a-f]{64}$'
       OR (p_value ? 'hidden_count' AND (
            NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                p_value -> 'hidden_count', 0, 2147483647
            )
       ))
       OR (p_value ? 'unsafe_count' AND (
            NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                p_value -> 'unsafe_count', 0, 2147483647
            )
       )) THEN
        RETURN false;
    END IF;

    v_kind := p_value ->> 'kind';
    IF v_kind NOT IN ('zip', 'scan') THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_value -> 'top_level_dirs') IS NOT NULL
       AND pg_catalog.jsonb_typeof(p_value -> 'top_level_dirs') <> 'array' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_array_length(
        COALESCE(p_value -> 'top_level_dirs', '[]'::jsonb)
    ) > 5000 OR EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(
            COALESCE(p_value -> 'top_level_dirs', '[]'::jsonb)
        ) AS value(item)
        WHERE pg_catalog.jsonb_typeof(value.item) <> 'string'
           OR value.item #>> '{}' = ''
           OR pg_catalog.octet_length(value.item #>> '{}') > 4096
           OR pg_catalog.strpos(value.item #>> '{}', '/') > 0
           OR pg_catalog.strpos(value.item #>> '{}', pg_catalog.chr(92)) > 0
           OR value.item #>> '{}' ~ '[[:cntrl:]]'
    ) OR (
        SELECT pg_catalog.count(*) <> pg_catalog.count(DISTINCT value.item #>> '{}')
        FROM pg_catalog.jsonb_array_elements(
            COALESCE(p_value -> 'top_level_dirs', '[]'::jsonb)
        ) AS value(item)
    ) THEN
        RETURN false;
    END IF;

    IF pg_catalog.jsonb_typeof(p_value -> 'hidden_count') = 'null'
       OR pg_catalog.jsonb_typeof(p_value -> 'unsafe_count') = 'null' THEN
        RETURN false;
    END IF;
    v_hidden := COALESCE((p_value ->> 'hidden_count')::bigint, 0);
    v_unsafe := COALESCE((p_value ->> 'unsafe_count')::bigint, 0);

    IF v_kind = 'scan' THEN
        RETURN pg_catalog.jsonb_typeof(p_value -> 'total_in_zip') = 'null'
           AND pg_catalog.jsonb_typeof(p_value -> 'planned_entries') = 'null'
           AND v_hidden = 0 AND v_unsafe = 0
           AND COALESCE(p_value -> 'top_level_dirs', '[]'::jsonb) = '[]'::jsonb;
    END IF;

    IF NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_value -> 'total_in_zip', 0, 5000
       ) OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_value -> 'planned_entries', 0, 5000
       ) THEN
        RETURN false;
    END IF;
    v_total := (p_value ->> 'total_in_zip')::bigint;
    v_planned := (p_value ->> 'planned_entries')::bigint;
    RETURN v_total <= 5000 AND v_planned <= 5000
       AND v_planned + v_hidden = v_total;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_descriptor_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_relative_path_valid(p_path text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_path IS NOT NULL
       AND pg_catalog.char_length(p_path) BETWEEN 1 AND 4096
       AND p_path !~ '^[/]'
       AND p_path !~ '//$'
       AND pg_catalog.strpos(p_path, '//') = 0
       AND pg_catalog.strpos(p_path, pg_catalog.chr(92)) = 0
       AND pg_catalog.right(p_path, 1) <> '/'
       AND p_path !~ '[[:cntrl:]]'
       AND p_path !~ '(^|/)([.][.]?)(/|$)'
       AND p_path !~ '(^|/)[A-Za-z]:($|/)'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_relative_path_valid(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_scan_file_type(p_relpath text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.strpos(leaf.name, '.') = 0 THEN 'other'
        WHEN pg_catalog.lower(pg_catalog.reverse(pg_catalog.split_part(
            pg_catalog.reverse(leaf.name), '.', 1
        ))) IN ('jpg', 'jpeg', 'png', 'bmp', 'webp', 'tiff', 'tif', 'gif', 'svg')
            THEN 'image'
        WHEN pg_catalog.lower(pg_catalog.reverse(pg_catalog.split_part(
            pg_catalog.reverse(leaf.name), '.', 1
        ))) IN ('mp4', 'avi', 'mov', 'mkv', 'wmv', 'flv', 'webm') THEN 'video'
        WHEN pg_catalog.lower(pg_catalog.reverse(pg_catalog.split_part(
            pg_catalog.reverse(leaf.name), '.', 1
        ))) IN ('pcd', 'bin', 'las', 'ply') THEN 'point_cloud'
        ELSE 'other'
    END
    FROM (
        SELECT pg_catalog.reverse(pg_catalog.split_part(
            pg_catalog.reverse(p_relpath), '/', 1
        )) AS name
    ) AS leaf
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_scan_file_type(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_source_valid(
    p_value jsonb, p_kind text, p_source_prefix text
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_width text;
    v_height text;
    v_expected_type text;
BEGIN
    IF p_value IS NULL OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF NOT (p_value ?& ARRAY[
            'source_name', 'relpath', 'source_key', 'file_type', 'content_type',
            'file_size', 'content_hash', 'width', 'height'
       ])
       OR (p_value - ARRAY[
            'source_name', 'relpath', 'source_key', 'file_type', 'content_type',
            'file_size', 'content_hash', 'width', 'height', 'error_code'
       ]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_value -> 'source_name') <> 'string'
       OR pg_catalog.char_length(p_value ->> 'source_name') NOT BETWEEN 1 AND 4096
       OR pg_catalog.jsonb_typeof(p_value -> 'relpath') <> 'string'
       OR pg_catalog.char_length(p_value ->> 'relpath') NOT BETWEEN 1 AND 4096
       OR pg_catalog.jsonb_typeof(p_value -> 'source_key') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_value -> 'source_key') = 'string'
           AND pg_catalog.char_length(p_value ->> 'source_key') NOT BETWEEN 1 AND 4096)
       OR pg_catalog.jsonb_typeof(p_value -> 'file_type') <> 'string'
       OR p_value ->> 'file_type' NOT IN ('image', 'video', 'point_cloud', 'other')
       OR pg_catalog.jsonb_typeof(p_value -> 'content_type') <> 'string'
       OR pg_catalog.char_length(p_value ->> 'content_type') > 255
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_value -> 'file_size', 0, 9223372036854775807
       )
       OR pg_catalog.jsonb_typeof(p_value -> 'content_hash') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_value -> 'content_hash') = 'string'
           AND p_value ->> 'content_hash' !~ '^[0-9a-f]{32}$')
       OR pg_catalog.jsonb_typeof(p_value -> 'width') NOT IN ('number', 'null')
       OR pg_catalog.jsonb_typeof(p_value -> 'height') NOT IN ('number', 'null') THEN
        RETURN false;
    END IF;

    v_width := p_value ->> 'width';
    v_height := p_value ->> 'height';
    IF (v_width IS NULL) <> (v_height IS NULL)
       OR (v_width IS NOT NULL AND (
            NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                p_value -> 'width', 1, 2147483647
            ) OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                p_value -> 'height', 1, 2147483647
            )
       ))
       OR (v_width IS NOT NULL AND p_value ->> 'file_type' <> 'image') THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_value -> 'error_code') NOT IN ('string', 'null')
       OR (p_value ? 'error_code' AND pg_catalog.jsonb_typeof(p_value -> 'error_code') = 'string'
           AND p_value ->> 'error_code' NOT IN (
                'invalid_source_object', 'archive_entry_read_failed',
                'storage_write_failed', 'source_changed', 'source_missing',
                'source_read_failed'
           )) THEN
        RETURN false;
    END IF;
    IF p_kind = 'zip' THEN
        v_expected_type := CASE
            WHEN pg_catalog.left(p_value ->> 'content_type', 6) = 'image/' THEN 'image'
            WHEN pg_catalog.left(p_value ->> 'content_type', 6) = 'video/' THEN 'video'
            ELSE 'other'
        END;
        RETURN pg_catalog.jsonb_typeof(p_value -> 'source_key') = 'null'
           AND p_value ->> 'file_type' = v_expected_type;
    END IF;
    v_expected_type := aap_assets_v1._dataset_source_ingest_scan_file_type(
        p_value ->> 'relpath'
    );
    RETURN p_kind = 'scan'
       AND pg_catalog.jsonb_typeof(p_value -> 'source_key') = 'string'
       AND p_value ->> 'source_key' = p_source_prefix || (p_value ->> 'relpath')
       AND p_value ->> 'file_type' = v_expected_type;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_source_valid(jsonb, text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_error_message(p_code text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE p_code
        WHEN 'invalid_source_object' THEN 'Invalid source object path'
        WHEN 'archive_entry_read_failed' THEN 'Archive entry could not be read'
        WHEN 'storage_write_failed' THEN 'Source object could not be stored'
        WHEN 'source_changed' THEN 'Source object changed during ingest'
        WHEN 'source_missing' THEN 'Source object is missing'
        WHEN 'source_read_failed' THEN 'Source object could not be read'
        ELSE NULL
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_error_message(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_context(p_operation_id uuid)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'operation_id', operation.operation_id,
        'actor_id', operation.actor_id,
        'dataset_id', operation.dataset_id,
        'descriptor', operation.descriptor,
        'state', operation.state,
        'dataset_name', operation.dataset_name,
        'is_temporal', operation.is_temporal,
        'source_prefix', operation.source_prefix,
        'next_index', operation.next_index,
        'result', operation.result
    )
    FROM public.dataset_source_ingest_operations AS operation
    WHERE operation.operation_id = p_operation_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_context(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_progress(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'operation_id', operation.operation_id,
        'actor_id', operation.actor_id,
        'dataset_id', operation.dataset_id,
        'staged', operation.next_index,
        'settled', operation.added + operation.deduped
            + operation.skipped + operation.error_count,
        'added', operation.added,
        'deduped', operation.deduped,
        'skipped', operation.skipped,
        'error_count', operation.error_count,
        'linked_tasks', operation.linked_tasks
    )
    FROM public.dataset_source_ingest_operations AS operation
    WHERE operation.operation_id = p_operation_id
      AND operation.actor_id = p_actor_id
      AND operation.dataset_id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_progress(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_dataset_snapshot(
    p_dataset_id uuid
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH item_facts AS MATERIALIZED (
        SELECT item.id, item.file_name, item.file_path, item.file_type,
            item.scene_id, item.frame_index,
            CASE
                WHEN pg_catalog.jsonb_typeof(
                    item.metadata -> 'source_ingest' -> 'operation_id'
                ) = 'string'
                AND pg_catalog.replace(pg_catalog.btrim(pg_catalog.replace(
                    pg_catalog.replace(item.metadata -> 'source_ingest' ->> 'operation_id',
                        'urn:', ''), 'uuid:', ''), '{}'), '-', '')
                    ~* '^[0-9a-f]{32}$'
                AND pg_catalog.jsonb_typeof(
                    item.metadata -> 'source_ingest' -> 'relpath'
                ) = 'string'
                AND aap_assets_v1._dataset_source_ingest_relative_path_valid(
                    item.metadata -> 'source_ingest' ->> 'relpath'
                )
                    THEN item.metadata -> 'source_ingest' ->> 'relpath'
                ELSE NULL
            END AS logical_path
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
    ),
    scene_facts AS MATERIALIZED (
        SELECT scene.id, scene.name, scene.source_format,
            scene.source_metadata, scene.created_by
        FROM public.scenes AS scene
        WHERE scene.dataset_id = p_dataset_id
    ),
    snapshot_value AS (
        SELECT pg_catalog.jsonb_build_object(
            'items', COALESCE((
                SELECT pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_array(
                        item.id, item.file_name, item.file_path, item.file_type,
                        item.scene_id, item.frame_index, item.logical_path
                    ) ORDER BY item.id
                )
                FROM item_facts AS item
            ), '[]'::jsonb),
            'scenes', COALESCE((
                SELECT pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_array(
                        scene.id, scene.name, scene.source_format,
                        scene.source_metadata, scene.created_by
                    ) ORDER BY scene.id
                )
                FROM scene_facts AS scene
            ), '[]'::jsonb)
        ) AS value
    )
    SELECT pg_catalog.jsonb_build_object(
        'snapshot_digest', pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(snapshot.value::text, 'UTF8')),
            'hex'
        ),
        'scene_count', (SELECT pg_catalog.count(*)::integer FROM scene_facts),
        'assigned_count', (
            SELECT pg_catalog.count(*)::integer
            FROM item_facts AS item WHERE item.scene_id IS NOT NULL
        ),
        'total_items', (SELECT pg_catalog.count(*)::integer FROM item_facts)
    )
    FROM snapshot_value AS snapshot
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_dataset_snapshot(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_batch_plan(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_batch_id uuid
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'operation_id', operation.operation_id,
        'actor_id', operation.actor_id,
        'dataset_id', operation.dataset_id,
        'batch_id', p_batch_id,
        'entries', COALESCE((
            SELECT pg_catalog.jsonb_agg(
                entry.source || pg_catalog.jsonb_build_object(
                    'entry_id', entry.entry_id,
                    'index', entry.entry_index,
                    'status', CASE WHEN entry.planned_status = 'pending'
                        THEN entry.status ELSE entry.planned_status END,
                    'item_id', entry.item_id,
                    'publication_id', entry.publication_id,
                    'file_name', entry.file_name,
                    'file_path', entry.file_path,
                    'error_code', entry.error_code
                ) ORDER BY entry.entry_index
            )
            FROM public.dataset_source_ingest_entries AS entry
            WHERE entry.operation_id = operation.operation_id
              AND entry.batch_id = p_batch_id
        ), '[]'::jsonb),
        'progress', aap_assets_v1._dataset_source_ingest_progress(
            p_operation_id, p_actor_id, p_dataset_id
        )
    )
    FROM public.dataset_source_ingest_operations AS operation
    WHERE operation.operation_id = p_operation_id
      AND operation.actor_id = p_actor_id
      AND operation.dataset_id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_batch_plan(uuid, uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_dataset_ingest(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_descriptor jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_code text;
    v_role text;
    v_descriptor jsonb;
    v_saved public.dataset_source_ingest_operations%ROWTYPE;
    v_dataset_name text;
    v_is_temporal boolean;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR NOT aap_assets_v1._dataset_source_ingest_descriptor_valid(p_descriptor) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;

    v_descriptor := p_descriptor || pg_catalog.jsonb_build_object(
        'hidden_count', COALESCE(p_descriptor -> 'hidden_count', '0'::jsonb),
        'unsafe_count', COALESCE(p_descriptor -> 'unsafe_count', '0'::jsonb),
        'top_level_dirs', COALESCE(p_descriptor -> 'top_level_dirs', '[]'::jsonb)
    );
    IF v_descriptor ->> 'kind' = 'scan'
       AND v_descriptor ->> 'request_digest' IS DISTINCT FROM
           'f5f47f290df98f65d1879eb9896f8bc6e16b4b0c805a834cca38c44a44faf065' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Assets source ingest Identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_actor #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy') THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, v_code);
        END IF;
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Assets source ingest Identity failure mismatch';
    END IF;
    IF pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'platform_role']) <> '{}'::jsonb
       OR NOT ((v_actor -> 'data') ?& ARRAY['actor_id', 'platform_role'])
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,actor_id}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Assets source ingest Identity facts mismatch';
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_account_role');
    END IF;
    IF v_descriptor ->> 'kind' = 'scan'
       AND v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'catalog_manager_required');
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-dataset-source-ingest:' || p_operation_id::text, 0
        )
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END IF;

    BEGIN
        SELECT operation.* INTO v_saved
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.dataset_id, v_saved.kind,
            v_saved.request_digest)
           IS DISTINCT FROM
           (p_actor_id, p_dataset_id, v_descriptor ->> 'kind',
            v_descriptor ->> 'request_digest') THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        IF v_saved.state = 'completed' THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(
                aap_assets_v1._dataset_source_ingest_context(p_operation_id), NULL
            );
        END IF;
        IF v_saved.creator_transaction_id <> pg_catalog.txid_current() THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            aap_assets_v1._dataset_source_ingest_context(p_operation_id), NULL
        );
    END IF;

    BEGIN
        SELECT dataset.name, dataset.is_temporal
        INTO v_dataset_name, v_is_temporal
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF pg_catalog.left(
        pg_catalog.split_part(pg_catalog.btrim(v_dataset_name, '/'), '/', 1),
        pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'reserved_namespace');
    END IF;
    IF v_descriptor ->> 'kind' = 'scan'
       AND pg_catalog.split_part(pg_catalog.btrim(v_dataset_name, '/'), '/', 1)
           IN ('dataset-sources', 'dataset-uploads') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'reserved_namespace');
    END IF;

    INSERT INTO public.dataset_source_ingest_operations (
        operation_id, actor_id, dataset_id, kind, request_digest, descriptor,
        state, creator_transaction_id, dataset_name, is_temporal, source_prefix
    ) VALUES (
        p_operation_id, p_actor_id, p_dataset_id,
        v_descriptor ->> 'kind', v_descriptor ->> 'request_digest', v_descriptor,
        'preparing', pg_catalog.txid_current(), v_dataset_name, v_is_temporal,
        v_dataset_name || '/'
    );
    RETURN aap_assets_v1._dataset_source_ingest_reply(
        aap_assets_v1._dataset_source_ingest_context(p_operation_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_dataset_ingest(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_dataset_ingest_batch(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_batch_id uuid,
    p_entries jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_input jsonb;
    v_source jsonb;
    v_sources jsonb := '[]'::jsonb;
    v_existing_sources jsonb;
    v_entry_id uuid;
    v_item_id uuid;
    v_publication_id uuid;
    v_duplicate uuid;
    v_index integer;
    v_total integer;
    v_ordinal bigint;
    v_status text;
    v_error_code text;
    v_relpath text;
    v_file_name text;
    v_file_path text;
    v_candidate text;
    v_leaf text;
    v_stem text;
    v_extension text;
    v_dot integer;
    v_suffix integer;
    v_added integer := 0;
    v_deduped integer := 0;
    v_skipped integer := 0;
    v_errors integer := 0;
    v_batch_count integer;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_batch_id IS NULL OR p_entries IS NULL
       OR pg_catalog.jsonb_typeof(p_entries) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(p_entries) > 200 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;

    BEGIN
        SELECT operation.* INTO v_operation
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF (v_operation.actor_id, v_operation.dataset_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current() THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    FOR v_input IN SELECT value FROM pg_catalog.jsonb_array_elements(p_entries)
    LOOP
        IF NOT aap_assets_v1._dataset_source_ingest_source_valid(
            v_input, v_operation.kind, v_operation.source_prefix
        ) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        v_source := pg_catalog.jsonb_build_object(
            'source_name', v_input -> 'source_name',
            'relpath', v_input -> 'relpath',
            'source_key', v_input -> 'source_key',
            'file_type', v_input -> 'file_type',
            'content_type', v_input -> 'content_type',
            'file_size', v_input -> 'file_size',
            'content_hash', v_input -> 'content_hash',
            'width', v_input -> 'width',
            'height', v_input -> 'height',
            'error_code', COALESCE(v_input -> 'error_code', 'null'::jsonb)
        );
        v_sources := v_sources || pg_catalog.jsonb_build_array(v_source);
    END LOOP;
    v_total := pg_catalog.jsonb_array_length(v_sources);
    SELECT pg_catalog.count(*)::integer INTO v_batch_count
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id AND entry.batch_id = p_batch_id;
    IF v_batch_count > 0 THEN
        SELECT COALESCE(pg_catalog.jsonb_agg(entry.source ORDER BY entry.entry_index), '[]'::jsonb)
        INTO v_existing_sources
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id AND entry.batch_id = p_batch_id;
        IF v_existing_sources IS DISTINCT FROM v_sources THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            aap_assets_v1._dataset_source_ingest_batch_plan(
                p_operation_id, p_actor_id, p_dataset_id, p_batch_id
            ), NULL
        );
    END IF;
    IF v_total = 0 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'actor_id', p_actor_id,
                'dataset_id', p_dataset_id,
                'batch_id', p_batch_id,
                'entries', '[]'::jsonb,
                'progress', aap_assets_v1._dataset_source_ingest_progress(
                    p_operation_id, p_actor_id, p_dataset_id
                )
            ), NULL
        );
    END IF;
    IF v_operation.next_index::bigint + v_total > 2147483647
       OR (v_operation.kind = 'zip' AND
           v_operation.next_index::bigint + v_total >
               (v_operation.descriptor ->> 'planned_entries')::bigint) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
    END IF;

    FOR v_input, v_ordinal IN
        SELECT value, ordinality
        FROM pg_catalog.jsonb_array_elements(v_sources) WITH ORDINALITY
    LOOP
        v_entry_id := pg_catalog.gen_random_uuid();
        v_index := v_operation.next_index + v_ordinal::integer - 1;
        v_status := 'pending';
        v_error_code := NULL;
        v_item_id := NULL;
        v_publication_id := NULL;
        v_file_name := NULL;
        v_file_path := NULL;
        v_relpath := v_input ->> 'relpath';

        IF pg_catalog.jsonb_typeof(v_input -> 'error_code') = 'string' THEN
            v_status := 'error';
            v_error_code := v_input ->> 'error_code';
        ELSIF NOT aap_assets_v1._dataset_source_ingest_relative_path_valid(v_relpath) THEN
            v_status := 'error';
            v_error_code := 'invalid_source_object';
        ELSIF v_operation.kind = 'scan' AND (
            EXISTS (
                SELECT 1
                FROM public.dataset_items AS item
                WHERE item.dataset_id = p_dataset_id
                  AND item.file_path = v_input ->> 'source_key'
            ) OR EXISTS (
                SELECT 1
                FROM public.dataset_source_ingest_entries AS prior
                WHERE prior.operation_id = p_operation_id
                  AND prior.source ->> 'source_key' = v_input ->> 'source_key'
                  AND prior.planned_status <> 'error'
            )
        ) THEN
            v_status := 'skipped';
        ELSIF v_input ->> 'content_hash' IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.dataset_items AS item
            WHERE item.dataset_id = p_dataset_id
              AND item.content_hash = v_input ->> 'content_hash'
        ) THEN
            v_status := 'deduped';
        ELSE
            v_item_id := pg_catalog.gen_random_uuid();
            IF v_operation.kind = 'zip' THEN
                v_publication_id := pg_catalog.gen_random_uuid();
                v_file_name := pg_catalog.reverse(
                    pg_catalog.split_part(pg_catalog.reverse(v_relpath), '/', 1)
                );
                v_file_path := 'dataset-sources/' || p_dataset_id::text || '/'
                    || v_item_id::text || '/' || v_publication_id::text || '/' || v_relpath;
                IF pg_catalog.char_length(v_file_name) NOT BETWEEN 1 AND 500
                   OR pg_catalog.char_length(v_file_path) > 1000
                   OR pg_catalog.octet_length(v_file_path) > 1024 THEN
                    v_status := 'error';
                    v_error_code := 'invalid_source_object';
                    v_item_id := NULL;
                    v_publication_id := NULL;
                    v_file_name := NULL;
                    v_file_path := NULL;
                END IF;
            ELSE
                v_candidate := v_relpath;
                v_leaf := pg_catalog.reverse(
                    pg_catalog.split_part(pg_catalog.reverse(v_candidate), '/', 1)
                );
                v_dot := pg_catalog.strpos(pg_catalog.reverse(v_leaf), '.');
                IF v_dot > 0 AND v_dot < pg_catalog.char_length(v_leaf) THEN
                    v_extension := pg_catalog.right(v_leaf, v_dot);
                    v_stem := pg_catalog.left(
                        v_candidate, pg_catalog.char_length(v_candidate) - v_dot
                    );
                ELSE
                    v_extension := '';
                    v_stem := v_candidate;
                END IF;
                v_suffix := 1;
                WHILE EXISTS (
                    SELECT 1 FROM public.dataset_items AS item
                    WHERE item.dataset_id = p_dataset_id
                      AND item.file_name = v_candidate
                ) OR EXISTS (
                    SELECT 1 FROM public.dataset_source_ingest_entries AS prior
                    WHERE prior.operation_id = p_operation_id
                      AND prior.file_name = v_candidate
                      AND prior.status IN ('pending', 'added')
                ) LOOP
                    v_candidate := v_stem || '-' || v_suffix::text || v_extension;
                    v_suffix := v_suffix + 1;
                    IF v_suffix = 2147483647 THEN
                        EXIT;
                    END IF;
                END LOOP;
                v_file_name := v_candidate;
                v_file_path := v_input ->> 'source_key';
                IF pg_catalog.char_length(v_file_name) NOT BETWEEN 1 AND 500
                   OR pg_catalog.char_length(v_file_path) > 1000
                   OR pg_catalog.octet_length(v_file_path) > 1024 THEN
                    v_status := 'error';
                    v_error_code := 'invalid_source_object';
                    v_item_id := NULL;
                    v_file_name := NULL;
                    v_file_path := NULL;
                END IF;
            END IF;
        END IF;

        INSERT INTO public.dataset_source_ingest_entries (
            operation_id, entry_id, entry_index, batch_id, source,
            planned_status, status, item_id, publication_id, file_name,
            file_path, error_code, settled_at
        ) VALUES (
            p_operation_id, v_entry_id, v_index, p_batch_id, v_input,
            v_status, v_status, v_item_id, v_publication_id, v_file_name,
            v_file_path, v_error_code,
            CASE WHEN v_status = 'pending' THEN NULL ELSE pg_catalog.clock_timestamp() END
        );
        IF v_status = 'deduped' THEN
            v_deduped := v_deduped + 1;
        ELSIF v_status = 'skipped' THEN
            v_skipped := v_skipped + 1;
        ELSIF v_status = 'error' THEN
            v_errors := v_errors + 1;
        END IF;
    END LOOP;

    IF v_operation.deduped::bigint + v_deduped > 2147483647
       OR v_operation.skipped::bigint + v_skipped > 2147483647
       OR v_operation.error_count::bigint + v_errors > 2147483647 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
    END IF;
    UPDATE public.dataset_source_ingest_operations AS operation
    SET next_index = operation.next_index + v_total,
        deduped = operation.deduped + v_deduped,
        skipped = operation.skipped + v_skipped,
        error_count = operation.error_count + v_errors
    WHERE operation.operation_id = p_operation_id;
    RETURN aap_assets_v1._dataset_source_ingest_reply(
        aap_assets_v1._dataset_source_ingest_batch_plan(
            p_operation_id, p_actor_id, p_dataset_id, p_batch_id
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_dataset_ingest_batch(uuid, uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_completion_valid(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_status text;
    v_file_size text;
    v_width text;
    v_height text;
BEGIN
    IF p_value IS NULL OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF NOT (p_value ?& ARRAY[
            'entry_id', 'status', 'publication_id', 'file_size',
            'content_hash', 'width', 'height', 'error_code'
       ])
       OR (p_value - ARRAY[
            'entry_id', 'status', 'publication_id', 'file_size',
            'content_hash', 'width', 'height', 'error_code'
       ]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_value -> 'entry_id') <> 'string'
       OR p_value ->> 'entry_id' !~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
       OR pg_catalog.jsonb_typeof(p_value -> 'status') <> 'string'
       OR p_value ->> 'status' NOT IN ('ready', 'error')
       OR pg_catalog.jsonb_typeof(p_value -> 'publication_id') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_value -> 'publication_id') = 'string'
           AND p_value ->> 'publication_id' !~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
       OR pg_catalog.jsonb_typeof(p_value -> 'file_size') NOT IN ('number', 'null')
       OR pg_catalog.jsonb_typeof(p_value -> 'content_hash') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_value -> 'content_hash') = 'string'
           AND p_value ->> 'content_hash' !~ '^[0-9a-f]{32}$')
       OR pg_catalog.jsonb_typeof(p_value -> 'width') NOT IN ('number', 'null')
       OR pg_catalog.jsonb_typeof(p_value -> 'height') NOT IN ('number', 'null')
       OR pg_catalog.jsonb_typeof(p_value -> 'error_code') NOT IN ('string', 'null') THEN
        RETURN false;
    END IF;
    v_status := p_value ->> 'status';
    v_file_size := p_value ->> 'file_size';
    v_width := p_value ->> 'width';
    v_height := p_value ->> 'height';
    IF v_status = 'ready' THEN
        RETURN aap_assets_v1._dataset_source_ingest_integer_valid(
                p_value -> 'file_size', 0, 9223372036854775807
           )
           AND pg_catalog.jsonb_typeof(p_value -> 'error_code') = 'null'
           AND ((v_width IS NULL) = (v_height IS NULL))
           AND (v_width IS NULL OR (
                aap_assets_v1._dataset_source_ingest_integer_valid(
                    p_value -> 'width', 1, 2147483647
                ) AND aap_assets_v1._dataset_source_ingest_integer_valid(
                    p_value -> 'height', 1, 2147483647
                )
           ));
    END IF;
    RETURN pg_catalog.jsonb_typeof(p_value -> 'file_size') = 'null'
       AND pg_catalog.jsonb_typeof(p_value -> 'content_hash') = 'null'
       AND pg_catalog.jsonb_typeof(p_value -> 'width') = 'null'
       AND pg_catalog.jsonb_typeof(p_value -> 'height') = 'null'
       AND pg_catalog.jsonb_typeof(p_value -> 'error_code') = 'string'
       AND p_value ->> 'error_code' IN (
            'invalid_source_object', 'archive_entry_read_failed',
            'storage_write_failed', 'source_changed', 'source_missing',
            'source_read_failed'
       );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_completion_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_dataset_ingest_batch(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_batch_id uuid,
    p_completions jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_entry public.dataset_source_ingest_entries%ROWTYPE;
    v_input jsonb;
    v_completion_set jsonb := '[]'::jsonb;
    v_expected_set jsonb;
    v_settlements jsonb := '[]'::jsonb;
    v_entry_count integer;
    v_pending_count integer;
    v_input_count integer;
    v_added_count integer := 0;
    v_deduped_count integer := 0;
    v_error_count integer := 0;
    v_linked_tasks integer := 0;
    v_batch_hashes text[] := ARRAY[]::text[];
    v_dataset_file_count integer;
    v_effective_status text;
    v_error_code text;
    v_added_ids uuid[] := ARRAY[]::uuid[];
    v_link_reply jsonb;
    v_project_entry jsonb;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_batch_id IS NULL OR p_completions IS NULL
       OR pg_catalog.jsonb_typeof(p_completions) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(p_completions) > 200 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT operation.* INTO v_operation
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF (v_operation.actor_id, v_operation.dataset_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current() THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    FOR v_input IN SELECT value FROM pg_catalog.jsonb_array_elements(p_completions)
    LOOP
        IF NOT aap_assets_v1._dataset_source_ingest_completion_valid(v_input) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_completion_set) AS prior(value)
            WHERE prior.value ->> 'entry_id' = v_input ->> 'entry_id'
        ) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        SELECT entry.* INTO v_entry
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id
          AND entry.batch_id = p_batch_id
          AND entry.entry_id = (v_input ->> 'entry_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        IF v_entry.planned_status = 'pending' THEN
            IF (v_operation.kind = 'zip' AND
                (v_input ->> 'publication_id')::uuid IS DISTINCT FROM v_entry.publication_id)
               OR (v_operation.kind = 'scan' AND
                pg_catalog.jsonb_typeof(v_input -> 'publication_id') IS DISTINCT FROM 'null') THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
            END IF;
            IF v_input ->> 'status' = 'ready' THEN
                IF (v_input ->> 'file_size')::bigint IS DISTINCT FROM
                        (v_entry.source ->> 'file_size')::bigint
                   OR v_input ->> 'content_hash' IS DISTINCT FROM
                        v_entry.source ->> 'content_hash'
                   OR (v_entry.source ->> 'width') IS NOT NULL
                      AND v_input ->> 'width' IS DISTINCT FROM v_entry.source ->> 'width'
                   OR (v_entry.source ->> 'height') IS NOT NULL
                      AND v_input ->> 'height' IS DISTINCT FROM v_entry.source ->> 'height'
                   OR v_entry.source ->> 'file_type' <> 'image'
                      AND (v_input -> 'width' <> 'null'::jsonb
                           OR v_input -> 'height' <> 'null'::jsonb) THEN
                    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
                END IF;
            END IF;
        ELSIF v_entry.completion IS NULL
           OR v_entry.completion IS DISTINCT FROM v_input THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        v_completion_set := v_completion_set || pg_catalog.jsonb_build_array(v_input);
    END LOOP;

    SELECT pg_catalog.count(*)::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'pending')::integer,
        COALESCE(
            pg_catalog.jsonb_agg(entry.completion ORDER BY entry.entry_id)
                FILTER (WHERE entry.completion IS NOT NULL),
            '[]'::jsonb
        )
    INTO v_entry_count, v_pending_count, v_expected_set
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id AND entry.batch_id = p_batch_id;
    v_input_count := pg_catalog.jsonb_array_length(v_completion_set);
    IF v_entry_count = 0 THEN
        IF v_input_count <> 0 THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'actor_id', p_actor_id,
                'dataset_id', p_dataset_id,
                'batch_id', p_batch_id,
                'progress', aap_assets_v1._dataset_source_ingest_progress(
                    p_operation_id, p_actor_id, p_dataset_id
                )
            ), NULL
        );
    END IF;
    IF v_pending_count = 0 THEN
        SELECT COALESCE(pg_catalog.jsonb_agg(value ORDER BY value ->> 'entry_id'), '[]'::jsonb)
        INTO v_completion_set
        FROM pg_catalog.jsonb_array_elements(v_completion_set) AS incoming(value);
        SELECT COALESCE(pg_catalog.jsonb_agg(value ORDER BY value ->> 'entry_id'), '[]'::jsonb)
        INTO v_expected_set
        FROM pg_catalog.jsonb_array_elements(v_expected_set) AS saved(value);
        IF v_completion_set IS DISTINCT FROM v_expected_set THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'actor_id', p_actor_id,
                'dataset_id', p_dataset_id,
                'batch_id', p_batch_id,
                'progress', aap_assets_v1._dataset_source_ingest_progress(
                    p_operation_id, p_actor_id, p_dataset_id
                )
            ), NULL
        );
    END IF;
    IF v_input_count <> v_pending_count THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    FOR v_input IN
        SELECT incoming.value
        FROM public.dataset_source_ingest_entries AS entry
        JOIN pg_catalog.jsonb_array_elements(v_completion_set) AS incoming(value)
          ON (incoming.value ->> 'entry_id')::uuid = entry.entry_id
        WHERE entry.operation_id = p_operation_id
          AND entry.batch_id = p_batch_id
          AND entry.status = 'pending'
        ORDER BY entry.entry_index
    LOOP
        SELECT entry.* INTO v_entry
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id
          AND entry.batch_id = p_batch_id
          AND entry.entry_id = (v_input ->> 'entry_id')::uuid;
        v_effective_status := v_input ->> 'status';
        v_error_code := v_input ->> 'error_code';
        IF v_effective_status = 'ready' THEN
            IF v_input ->> 'content_hash' IS NOT NULL AND (
                EXISTS (
                    SELECT 1 FROM public.dataset_items AS item
                    WHERE item.dataset_id = p_dataset_id
                      AND item.content_hash = v_input ->> 'content_hash'
                ) OR pg_catalog.array_position(
                    v_batch_hashes, v_input ->> 'content_hash'
                ) IS NOT NULL
            ) THEN
                v_effective_status := 'deduped';
                v_deduped_count := v_deduped_count + 1;
            ELSE
                v_effective_status := 'added';
                v_added_count := v_added_count + 1;
                v_added_ids := pg_catalog.array_append(v_added_ids, v_entry.item_id);
                IF v_input ->> 'content_hash' IS NOT NULL THEN
                    v_batch_hashes := pg_catalog.array_append(
                        v_batch_hashes, v_input ->> 'content_hash'
                    );
                END IF;
            END IF;
            v_error_code := NULL;
        ELSE
            v_error_count := v_error_count + 1;
        END IF;
        v_settlements := v_settlements || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'entry_id', v_entry.entry_id,
                'status', v_effective_status,
                'publication_id', v_input -> 'publication_id',
                'file_size', v_input -> 'file_size',
                'content_hash', v_input -> 'content_hash',
                'width', v_input -> 'width',
                'height', v_input -> 'height',
                'error_code', v_error_code,
                'completion', v_input
            )
        );
    END LOOP;

    IF v_added_count::bigint + v_operation.added > 2147483647
       OR v_deduped_count::bigint + v_operation.deduped > 2147483647
       OR v_error_count::bigint + v_operation.error_count > 2147483647 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
    END IF;
    SELECT dataset.file_count INTO v_dataset_file_count
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF COALESCE(v_dataset_file_count, 0)::bigint + v_added_count > 2147483647 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF v_added_count > 0 AND EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
        JOIN public.dataset_source_ingest_entries AS entry
          ON entry.entry_id = (settlement.value ->> 'entry_id')::uuid
        JOIN public.dataset_items AS item
          ON item.id = entry.item_id OR item.file_path = entry.file_path
        WHERE entry.operation_id = p_operation_id
          AND settlement.value ->> 'status' = 'added'
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END IF;

    IF v_added_count > 0 THEN
        UPDATE public.datasets AS dataset
        SET file_count = COALESCE(dataset.file_count, 0) + v_added_count,
            updated_at = v_now
        WHERE dataset.id = p_dataset_id;
        INSERT INTO public.dataset_items (
            id, dataset_id, file_name, file_path, file_type, file_size,
            content_hash, width, height, metadata
        )
        SELECT entry.item_id,
            p_dataset_id,
            entry.file_name,
            entry.file_path,
            entry.source ->> 'file_type',
            (settlement.value ->> 'file_size')::bigint,
            settlement.value ->> 'content_hash',
            (settlement.value ->> 'width')::integer,
            (settlement.value ->> 'height')::integer,
            CASE WHEN v_operation.kind = 'zip' THEN
                pg_catalog.jsonb_build_object(
                    'source_ingest', pg_catalog.jsonb_build_object(
                        'operation_id', p_operation_id,
                        'relpath', entry.source ->> 'relpath'
                    )
                )
            ELSE '{}'::jsonb END
        FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
        JOIN public.dataset_source_ingest_entries AS entry
          ON entry.operation_id = p_operation_id
         AND entry.entry_id = (settlement.value ->> 'entry_id')::uuid
        WHERE settlement.value ->> 'status' = 'added'
        ORDER BY entry.entry_index;
    END IF;

    v_link_reply := aap_labeling_v1.append_dataset_items(
        p_dataset_id, v_added_ids, p_batch_id
    );
    IF pg_catalog.jsonb_typeof(v_link_reply) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Labeling append capability returned a non-object';
    END IF;
    IF (v_link_reply - ARRAY[
            'schema', 'operation_id', 'dataset_id', 'created',
            'project_totals', 'associations'
       ]) <> '{}'::jsonb
       OR NOT (v_link_reply ?& ARRAY[
            'schema', 'operation_id', 'dataset_id', 'created',
            'project_totals', 'associations'
       ])
       OR v_link_reply ->> 'schema' IS DISTINCT FROM 'aap-task-append/v1'
       OR v_link_reply ->> 'operation_id' IS DISTINCT FROM p_batch_id::text
       OR v_link_reply ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            v_link_reply -> 'created', 0, 2147483647
       )
       OR pg_catalog.jsonb_typeof(v_link_reply -> 'project_totals') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_link_reply -> 'associations') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Labeling append capability contract mismatch';
    END IF;
    v_linked_tasks := (v_link_reply ->> 'created')::integer;
    FOR v_project_entry IN
        SELECT value FROM pg_catalog.jsonb_array_elements(v_link_reply -> 'project_totals')
    LOOP
        IF pg_catalog.jsonb_typeof(v_project_entry) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append project projection is not an object';
        END IF;
        IF (v_project_entry - ARRAY['project_id', 'total_tasks']) <> '{}'::jsonb
           OR NOT (v_project_entry ?& ARRAY['project_id', 'total_tasks'])
           OR pg_catalog.jsonb_typeof(v_project_entry -> 'project_id') IS DISTINCT FROM 'string'
           OR v_project_entry ->> 'project_id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                v_project_entry -> 'total_tasks', 0, 2147483647
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append project projection mismatch';
        END IF;
    END LOOP;
    FOR v_project_entry IN
        SELECT value FROM pg_catalog.jsonb_array_elements(v_link_reply -> 'associations')
    LOOP
        IF pg_catalog.jsonb_typeof(v_project_entry) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append association projection is not an object';
        END IF;
        IF (v_project_entry - ARRAY['id', 'project_id']) <> '{}'::jsonb
           OR NOT (v_project_entry ?& ARRAY['id', 'project_id'])
           OR pg_catalog.jsonb_typeof(v_project_entry -> 'id') IS DISTINCT FROM 'string'
           OR v_project_entry ->> 'id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_project_entry -> 'project_id') IS DISTINCT FROM 'string'
           OR v_project_entry ->> 'project_id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append association projection mismatch';
        END IF;
    END LOOP;
    IF v_operation.linked_tasks::bigint + v_linked_tasks > 2147483647 THEN
        RAISE EXCEPTION USING ERRCODE = '22003',
            MESSAGE = 'Assets source ingest linked task count exceeds the wire contract';
    END IF;

    IF v_added_count > 0 THEN
        INSERT INTO public.dataset_source_ingest_deliveries (
            operation_id, kind, target_id
        )
        SELECT p_operation_id,
            CASE entry.source ->> 'file_type'
                WHEN 'image' THEN 'thumbnail'
                WHEN 'video' THEN 'video_metadata'
            END,
            entry.item_id
        FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
        JOIN public.dataset_source_ingest_entries AS entry
          ON entry.operation_id = p_operation_id
         AND entry.entry_id = (settlement.value ->> 'entry_id')::uuid
        WHERE settlement.value ->> 'status' = 'added'
          AND entry.source ->> 'file_type' IN ('image', 'video');
    END IF;

    UPDATE public.dataset_source_ingest_entries AS entry
    SET status = settlement.value ->> 'status',
        error_code = settlement.value ->> 'error_code',
        completion = settlement.value -> 'completion',
        file_size = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN (settlement.value ->> 'file_size')::bigint ELSE NULL END,
        content_hash = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN settlement.value ->> 'content_hash' ELSE NULL END,
        width = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN (settlement.value ->> 'width')::integer ELSE NULL END,
        height = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN (settlement.value ->> 'height')::integer ELSE NULL END,
        settled_at = v_now
    FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
    WHERE entry.operation_id = p_operation_id
      AND entry.entry_id = (settlement.value ->> 'entry_id')::uuid;

    UPDATE public.dataset_source_ingest_operations AS operation
    SET added = operation.added + v_added_count,
        deduped = operation.deduped + v_deduped_count,
        error_count = operation.error_count + v_error_count,
        linked_tasks = operation.linked_tasks + v_linked_tasks
    WHERE operation.operation_id = p_operation_id;

    RETURN aap_assets_v1._dataset_source_ingest_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'batch_id', p_batch_id,
            'progress', aap_assets_v1._dataset_source_ingest_progress(
                p_operation_id, p_actor_id, p_dataset_id
            )
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_dataset_ingest_batch(uuid, uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_dataset_ingest_layout(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_after_item_id uuid,
    p_limit integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_dataset_name text;
    v_is_temporal boolean;
    v_snapshot jsonb;
    v_items jsonb;
    v_next_item_id uuid;
    v_expected_entries integer;
    v_pending_entries integer;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT operation.* INTO v_operation
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF (v_operation.actor_id, v_operation.dataset_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current()
       OR v_operation.kind <> 'zip' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    v_expected_entries := (v_operation.descriptor ->> 'planned_entries')::integer;
    SELECT pg_catalog.count(*) FILTER (WHERE entry.status = 'pending')::integer
    INTO v_pending_entries
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id;
    IF v_operation.next_index <> v_expected_entries OR v_pending_entries <> 0
       OR v_operation.added = 0 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    BEGIN
        SELECT dataset.name, dataset.is_temporal
        INTO v_dataset_name, v_is_temporal
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF v_dataset_name IS DISTINCT FROM v_operation.dataset_name
       OR v_is_temporal IS DISTINCT FROM v_operation.is_temporal THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF p_after_item_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id AND item.id = p_after_item_id
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;

    IF v_operation.layout_snapshot_digest IS NULL THEN
        PERFORM item.id
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
        ORDER BY item.id
        FOR SHARE NOWAIT;
        PERFORM scene.id
        FROM public.scenes AS scene
        WHERE scene.dataset_id = p_dataset_id
        ORDER BY scene.id
        FOR SHARE NOWAIT;
        v_snapshot := aap_assets_v1._dataset_source_ingest_dataset_snapshot(p_dataset_id);
        UPDATE public.dataset_source_ingest_operations AS operation
        SET layout_snapshot_digest = v_snapshot ->> 'snapshot_digest',
            layout_scene_count = (v_snapshot ->> 'scene_count')::integer,
            layout_assigned_count = (v_snapshot ->> 'assigned_count')::integer,
            layout_total_items = (v_snapshot ->> 'total_items')::integer
        WHERE operation.operation_id = p_operation_id;
    ELSE
        v_snapshot := pg_catalog.jsonb_build_object(
            'snapshot_digest', v_operation.layout_snapshot_digest,
            'scene_count', v_operation.layout_scene_count,
            'assigned_count', v_operation.layout_assigned_count,
            'total_items', v_operation.layout_total_items
        );
    END IF;

    WITH candidates AS MATERIALIZED (
        SELECT item.id, item.file_name, item.file_path, item.file_type,
            item.scene_id, item.frame_index,
            CASE
                WHEN pg_catalog.jsonb_typeof(
                    item.metadata -> 'source_ingest' -> 'operation_id'
                ) = 'string'
                AND pg_catalog.replace(pg_catalog.btrim(pg_catalog.replace(
                    pg_catalog.replace(item.metadata -> 'source_ingest' ->> 'operation_id',
                        'urn:', ''), 'uuid:', ''), '{}'), '-', '')
                    ~* '^[0-9a-f]{32}$'
                AND pg_catalog.jsonb_typeof(
                    item.metadata -> 'source_ingest' -> 'relpath'
                ) = 'string'
                AND aap_assets_v1._dataset_source_ingest_relative_path_valid(
                    item.metadata -> 'source_ingest' ->> 'relpath'
                )
                    THEN item.metadata -> 'source_ingest' ->> 'relpath'
                ELSE NULL
            END AS logical_path,
            pg_catalog.row_number() OVER (ORDER BY item.id) AS ordinal
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
          AND (p_after_item_id IS NULL OR item.id > p_after_item_id)
        ORDER BY item.id
        LIMIT p_limit + 1
    )
    SELECT COALESCE(pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', candidate.id,
                'file_name', candidate.file_name,
                'file_path', candidate.file_path,
                'file_type', candidate.file_type,
                'scene_id', candidate.scene_id,
                'frame_index', candidate.frame_index,
                'logical_path', candidate.logical_path
            ) ORDER BY candidate.ordinal
        ) FILTER (WHERE candidate.ordinal <= p_limit), '[]'::jsonb),
        CASE WHEN pg_catalog.max(candidate.ordinal) > p_limit THEN
            (SELECT last_page.id FROM candidates AS last_page
             WHERE last_page.ordinal = p_limit)
        ELSE NULL END
    INTO v_items, v_next_item_id
    FROM candidates AS candidate;

    RETURN aap_assets_v1._dataset_source_ingest_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'snapshot_digest', v_snapshot ->> 'snapshot_digest',
            'scene_count', (v_snapshot ->> 'scene_count')::integer,
            'assigned_count', (v_snapshot ->> 'assigned_count')::integer,
            'total_items', (v_snapshot ->> 'total_items')::integer,
            'items', v_items,
            'next_item_id', v_next_item_id
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_dataset_ingest_layout(uuid, uuid, uuid, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_scene_plan_valid(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scene jsonb;
    v_assignment jsonb;
    v_scene_count integer;
    v_assignment_count integer;
    v_created integer;
    v_assigned integer;
BEGIN
    IF p_value IS NULL OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF (p_value - ARRAY[
            'mode', 'dry_run', 'scenes', 'assignments',
            'created_scenes', 'assigned_items', 'skipped_items', 'notes'
       ]) <> '{}'::jsonb
       OR NOT (p_value ?& ARRAY[
            'mode', 'dry_run', 'scenes', 'assignments',
            'created_scenes', 'assigned_items', 'skipped_items', 'notes'
       ])
       OR pg_catalog.jsonb_typeof(p_value -> 'mode') <> 'string'
       OR p_value ->> 'mode' NOT IN ('single', 'per_subdirectory', 'auto')
       OR pg_catalog.jsonb_typeof(p_value -> 'dry_run') <> 'boolean'
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_value -> 'created_scenes', 0, 2147483647
       )
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_value -> 'assigned_items', 0, 2147483647
       )
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_value -> 'skipped_items', 0, 2147483647
       ) THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_value -> 'scenes') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_value -> 'assignments') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_value -> 'notes') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_array_length(p_value -> 'scenes') > 100
       OR pg_catalog.jsonb_array_length(p_value -> 'notes') > 1024 THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_value -> 'notes') AS note(value)
        WHERE pg_catalog.jsonb_typeof(note.value) <> 'string'
    ) THEN
        RETURN false;
    END IF;
    v_scene_count := pg_catalog.jsonb_array_length(p_value -> 'scenes');
    v_assignment_count := pg_catalog.jsonb_array_length(p_value -> 'assignments');
    v_created := (p_value ->> 'created_scenes')::integer;
    v_assigned := (p_value ->> 'assigned_items')::integer;
    IF p_value -> 'dry_run' = 'false'::jsonb
       AND (v_created <> v_scene_count OR v_assigned <> v_assignment_count) THEN
        RETURN false;
    END IF;

    FOR v_scene IN SELECT value FROM pg_catalog.jsonb_array_elements(p_value -> 'scenes')
    LOOP
        IF pg_catalog.jsonb_typeof(v_scene) IS DISTINCT FROM 'object' THEN
            RETURN false;
        END IF;
        IF (v_scene - ARRAY['id', 'name', 'source_format', 'source_metadata']) <> '{}'::jsonb
           OR NOT (v_scene ?& ARRAY['id', 'name', 'source_format', 'source_metadata'])
           OR pg_catalog.jsonb_typeof(v_scene -> 'id') IS DISTINCT FROM 'string'
           OR v_scene ->> 'id' !~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
           OR pg_catalog.jsonb_typeof(v_scene -> 'name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_scene ->> 'name') > 255
           OR pg_catalog.jsonb_typeof(v_scene -> 'source_format') IS DISTINCT FROM 'string'
           OR v_scene ->> 'source_format' <> 'inferred'
           OR pg_catalog.jsonb_typeof(v_scene -> 'source_metadata') IS DISTINCT FROM 'object' THEN
            RETURN false;
        END IF;
        IF (v_scene -> 'source_metadata') - ARRAY['mode', 'group_key'] <> '{}'::jsonb
           OR NOT ((v_scene -> 'source_metadata') ?& ARRAY['mode', 'group_key'])
           OR pg_catalog.jsonb_typeof(v_scene #> '{source_metadata,mode}') IS DISTINCT FROM 'string'
           OR v_scene #>> '{source_metadata,mode}' NOT IN ('single', 'per_subdirectory', 'auto')
           OR pg_catalog.jsonb_typeof(v_scene #> '{source_metadata,group_key}') IS DISTINCT FROM 'string' THEN
            RETURN false;
        END IF;
    END LOOP;
    IF (
        SELECT pg_catalog.count(*) <> pg_catalog.count(DISTINCT pg_catalog.lower(scene.value ->> 'id'))
        FROM pg_catalog.jsonb_array_elements(p_value -> 'scenes') AS scene(value)
    ) OR (
        SELECT pg_catalog.count(*) <> pg_catalog.count(DISTINCT scene.value ->> 'name')
        FROM pg_catalog.jsonb_array_elements(p_value -> 'scenes') AS scene(value)
    ) THEN
        RETURN false;
    END IF;

    FOR v_assignment IN
        SELECT value FROM pg_catalog.jsonb_array_elements(p_value -> 'assignments')
    LOOP
        IF pg_catalog.jsonb_typeof(v_assignment) IS DISTINCT FROM 'object' THEN
            RETURN false;
        END IF;
        IF (v_assignment - ARRAY['item_id', 'scene_id', 'frame_index']) <> '{}'::jsonb
           OR NOT (v_assignment ?& ARRAY['item_id', 'scene_id', 'frame_index'])
           OR pg_catalog.jsonb_typeof(v_assignment -> 'item_id') IS DISTINCT FROM 'string'
           OR v_assignment ->> 'item_id' !~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
           OR pg_catalog.jsonb_typeof(v_assignment -> 'scene_id') IS DISTINCT FROM 'string'
           OR v_assignment ->> 'scene_id' !~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
           OR pg_catalog.jsonb_typeof(v_assignment -> 'frame_index') NOT IN ('number', 'null') THEN
            RETURN false;
        END IF;
        IF pg_catalog.jsonb_typeof(v_assignment -> 'frame_index') = 'number'
           AND NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                v_assignment -> 'frame_index', 0, 2147483647
           ) THEN
            RETURN false;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_value -> 'scenes') AS scene(value)
            WHERE pg_catalog.lower(scene.value ->> 'id') =
                pg_catalog.lower(v_assignment ->> 'scene_id')
        ) THEN
            RETURN false;
        END IF;
    END LOOP;
    IF (
        SELECT pg_catalog.count(*) <> pg_catalog.count(DISTINCT pg_catalog.lower(assignment.value ->> 'item_id'))
        FROM pg_catalog.jsonb_array_elements(p_value -> 'assignments') AS assignment(value)
    ) THEN
        RETURN false;
    END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_scene_plan_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_dataset_ingest(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_finish jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_dataset_name text;
    v_is_temporal boolean;
    v_finish_notes jsonb;
    v_scene_plan jsonb;
    v_layout_digest text;
    v_snapshot jsonb;
    v_errors jsonb := '[]'::jsonb;
    v_receipt jsonb;
    v_pre_scene_count integer;
    v_entry_count integer;
    v_pending_count integer;
    v_actual_added integer;
    v_actual_deduped integer;
    v_actual_skipped integer;
    v_actual_errors integer;
    v_skipped_count bigint;
    v_expected_entries integer;
    v_assignment_count integer;
    v_updated_count integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_finish IS NULL OR pg_catalog.jsonb_typeof(p_finish) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF (p_finish - ARRAY[
            'layout_snapshot_digest', 'scene_plan', 'scene_inference_notes'
       ]) <> '{}'::jsonb
       OR NOT (p_finish ?& ARRAY['layout_snapshot_digest', 'scene_plan']) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(p_finish -> 'layout_snapshot_digest') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_finish -> 'layout_snapshot_digest') = 'string'
           AND p_finish ->> 'layout_snapshot_digest' !~ '^[0-9a-f]{64}$')
       OR pg_catalog.jsonb_typeof(p_finish -> 'scene_plan') NOT IN ('object', 'null') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF p_finish ? 'scene_inference_notes'
       AND pg_catalog.jsonb_typeof(p_finish -> 'scene_inference_notes') IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    v_finish_notes := COALESCE(p_finish -> 'scene_inference_notes', '[]'::jsonb);
    IF pg_catalog.jsonb_array_length(v_finish_notes) > 1024 OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_finish_notes) AS note(value)
        WHERE pg_catalog.jsonb_typeof(note.value) IS DISTINCT FROM 'string'
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    v_scene_plan := CASE
        WHEN pg_catalog.jsonb_typeof(p_finish -> 'scene_plan') = 'object'
            THEN p_finish -> 'scene_plan'
        ELSE NULL
    END;
    v_layout_digest := p_finish ->> 'layout_snapshot_digest';
    IF v_scene_plan IS NOT NULL AND NOT aap_assets_v1._dataset_source_ingest_scene_plan_valid(v_scene_plan) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;

    BEGIN
        SELECT operation.* INTO v_operation
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF (v_operation.actor_id, v_operation.dataset_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current() THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    BEGIN
        SELECT dataset.name, dataset.is_temporal
        INTO v_dataset_name, v_is_temporal
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF v_dataset_name IS DISTINCT FROM v_operation.dataset_name
       OR v_is_temporal IS DISTINCT FROM v_operation.is_temporal THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    SELECT
        pg_catalog.count(*)::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'pending')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'added')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'deduped')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'skipped')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'error')::integer
    INTO v_entry_count, v_pending_count, v_actual_added, v_actual_deduped,
        v_actual_skipped, v_actual_errors
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id;
    IF v_pending_count <> 0 OR v_entry_count <> v_operation.next_index
       OR v_operation.added <> v_actual_added
       OR v_operation.deduped <> v_actual_deduped
       OR v_operation.skipped <> v_actual_skipped
       OR v_operation.error_count <> v_actual_errors THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.kind = 'zip' THEN
        v_expected_entries := (v_operation.descriptor ->> 'planned_entries')::integer;
        IF v_operation.next_index <> v_expected_entries THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        v_skipped_count := v_operation.skipped::bigint
            + (v_operation.descriptor ->> 'hidden_count')::bigint
            + (v_operation.descriptor ->> 'unsafe_count')::bigint;
        IF v_skipped_count > 2147483647 THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'name', pg_catalog.left(entry.source ->> 'source_name', 512),
                    'error', aap_assets_v1._dataset_source_ingest_error_message(entry.error_code)
                ) ORDER BY entry.entry_index
            ), '[]'::jsonb)
        INTO v_errors
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id AND entry.status = 'error';
        IF pg_catalog.jsonb_array_length(v_errors) <> v_operation.error_count
           OR pg_catalog.jsonb_array_length(v_errors) > 5000
           OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_errors) AS error(value)
                WHERE pg_catalog.jsonb_typeof(error.value -> 'name') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(error.value -> 'error') IS DISTINCT FROM 'string'
                   OR error.value ->> 'error' = ''
           ) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_response');
        END IF;
    ELSE
        IF v_scene_plan IS NOT NULL OR v_layout_digest IS NOT NULL
           OR pg_catalog.jsonb_array_length(v_finish_notes) <> 0 THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        v_skipped_count := v_operation.skipped;
        v_errors := '[]'::jsonb;
    END IF;

    IF v_operation.kind = 'zip' AND v_operation.added > 0 THEN
        IF v_operation.layout_snapshot_digest IS NULL
           OR v_layout_digest IS DISTINCT FROM v_operation.layout_snapshot_digest THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        v_snapshot := aap_assets_v1._dataset_source_ingest_dataset_snapshot(p_dataset_id);
        IF (v_operation.layout_snapshot_digest,
            v_operation.layout_scene_count,
            v_operation.layout_assigned_count,
            v_operation.layout_total_items)
           IS DISTINCT FROM
           (v_snapshot ->> 'snapshot_digest',
            (v_snapshot ->> 'scene_count')::integer,
            (v_snapshot ->> 'assigned_count')::integer,
            (v_snapshot ->> 'total_items')::integer) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
    ELSIF v_layout_digest IS NOT NULL OR v_scene_plan IS NOT NULL THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    IF v_operation.kind = 'zip' THEN
        IF v_scene_plan IS NOT NULL THEN
            IF v_scene_plan ->> 'mode' <> 'auto'
               OR v_scene_plan -> 'dry_run' IS DISTINCT FROM 'false'::jsonb THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
            END IF;
            IF v_operation.added = 0 THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
            END IF;
            v_assignment_count := pg_catalog.jsonb_array_length(v_scene_plan -> 'assignments');
            IF (v_scene_plan ->> 'assigned_items')::integer >
                    (v_operation.layout_total_items)::integer THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
            END IF;
            IF v_operation.layout_scene_count > 0
               AND (pg_catalog.jsonb_array_length(v_scene_plan -> 'scenes') > 0
                    OR v_assignment_count > 0) THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'scenes') AS planned(value)
                JOIN public.scenes AS scene
                  ON scene.id = (planned.value ->> 'id')::uuid
                     OR (scene.dataset_id = p_dataset_id
                         AND scene.name = planned.value ->> 'name')
            ) THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'assignments') AS assignment(value)
                LEFT JOIN public.dataset_items AS item
                  ON item.id = (assignment.value ->> 'item_id')::uuid
                WHERE item.id IS NULL OR item.dataset_id <> p_dataset_id
                   OR item.scene_id IS NOT NULL
            ) THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
            END IF;
        END IF;

        SELECT pg_catalog.count(*)::integer INTO v_pre_scene_count
        FROM public.scenes AS scene WHERE scene.dataset_id = p_dataset_id;
        IF v_is_temporal AND v_pre_scene_count = 0
           AND (v_scene_plan IS NULL
                OR pg_catalog.jsonb_array_length(v_scene_plan -> 'scenes') = 0) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'temporal_scenes_missing');
        END IF;
    END IF;

    v_receipt := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'kind', v_operation.kind,
        'request_digest', v_operation.request_digest,
        'added', v_operation.added,
        'deduped', v_operation.deduped,
        'skipped', v_skipped_count,
        'errors', v_errors,
        'error_count', v_operation.error_count,
        'total_in_zip', CASE WHEN v_operation.kind = 'zip'
            THEN (v_operation.descriptor -> 'total_in_zip') ELSE 'null'::jsonb END,
        'linked_tasks', v_operation.linked_tasks,
        'scene_inference_notes', CASE WHEN v_operation.kind = 'zip'
            THEN v_finish_notes ELSE '[]'::jsonb END
    );

    IF v_scene_plan IS NOT NULL THEN
        INSERT INTO public.scenes (
            id, display_id, dataset_id, name, source_format, source_metadata, created_by
        )
        SELECT (planned.value ->> 'id')::uuid,
            'SCN-' || pg_catalog.nextval('public.display_seq_scenes'::pg_catalog.regclass)::text,
            p_dataset_id,
            planned.value ->> 'name',
            planned.value ->> 'source_format',
            planned.value -> 'source_metadata',
            p_actor_id
        FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'scenes') AS planned(value)
        ORDER BY planned.value ->> 'id';

        UPDATE public.dataset_items AS item
        SET scene_id = (assignment.value ->> 'scene_id')::uuid,
            frame_index = (assignment.value ->> 'frame_index')::integer,
            updated_at = v_now
        FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'assignments') AS assignment(value)
        WHERE item.id = (assignment.value ->> 'item_id')::uuid
          AND item.dataset_id = p_dataset_id
          AND item.scene_id IS NULL;
        GET DIAGNOSTICS v_updated_count = ROW_COUNT;
        IF v_updated_count <> (v_scene_plan ->> 'assigned_items')::integer THEN
            RAISE EXCEPTION USING ERRCODE = '40001',
                MESSAGE = 'Assets source ingest scene snapshot changed';
        END IF;
    END IF;

    UPDATE public.dataset_source_ingest_operations AS operation
    SET state = 'completed',
        result = v_receipt,
        completed_at = v_now
    WHERE operation.operation_id = p_operation_id;
    RETURN aap_assets_v1._dataset_source_ingest_reply(v_receipt, NULL);
EXCEPTION
    WHEN unique_violation THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_dataset_ingest(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
