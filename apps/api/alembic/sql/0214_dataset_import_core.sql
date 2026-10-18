-- Assets-owned accepted connector-import operations and atomic settlements.
CREATE TABLE public.dataset_import_operations (
    job_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    connection_name varchar(255) NOT NULL,
    connection_kind varchar(20) NOT NULL,
    connection_scope varchar(20) NOT NULL,
    connection_created_by uuid,
    connection_updated_at timestamptz NOT NULL,
    connection_config jsonb NOT NULL,
    connection_secret_enc bytea NOT NULL,
    request jsonb NOT NULL,
    settings_snapshot jsonb NOT NULL,
    stage varchar(16) NOT NULL,
    cursor integer NOT NULL DEFAULT 0,
    total integer,
    total_bytes bigint,
    added integer NOT NULL DEFAULT 0,
    skipped integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    linked_tasks integer NOT NULL DEFAULT 0,
    committed_bytes bigint NOT NULL DEFAULT 0,
    lease_token uuid,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT ck_dataset_import_operations_connection_kind CHECK (
        connection_kind IN ('s3', 'sftp')
    ),
    CONSTRAINT ck_dataset_import_operations_connection_scope CHECK (
        connection_scope IN ('global', 'owner')
    ),
    CONSTRAINT ck_dataset_import_operations_stage CHECK (
        stage IN ('enumerating', 'importing', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT ck_dataset_import_operations_connection_config_object CHECK (
        pg_catalog.jsonb_typeof(connection_config) = 'object'
    ),
    CONSTRAINT ck_dataset_import_operations_request_object CHECK (
        pg_catalog.jsonb_typeof(request) = 'object'
    ),
    CONSTRAINT ck_dataset_import_operations_settings_snapshot_object CHECK (
        pg_catalog.jsonb_typeof(settings_snapshot) = 'object'
    ),
    CONSTRAINT ck_dataset_import_operations_secret_size CHECK (
        pg_catalog.octet_length(connection_secret_enc) BETWEEN 1 AND 49152
    ),
    CONSTRAINT ck_dataset_import_operations_cursor CHECK (cursor >= 0),
    CONSTRAINT ck_dataset_import_operations_counts CHECK (
        added >= 0 AND skipped >= 0 AND error_count >= 0 AND linked_tasks >= 0
    ),
    CONSTRAINT ck_dataset_import_operations_committed_bytes CHECK (
        committed_bytes >= 0
    ),
    CONSTRAINT ck_dataset_import_operations_total CHECK (
        total IS NULL OR (cursor <= total AND total >= 0)
    ),
    CONSTRAINT ck_dataset_import_operations_total_bytes CHECK (
        total_bytes IS NULL OR total_bytes >= 0
    ),
    CONSTRAINT ck_dataset_import_operations_manifest_pair CHECK (
        (total IS NULL) = (total_bytes IS NULL)
    ),
    CONSTRAINT ck_dataset_import_operations_outcome_cursor CHECK (
        cursor = added + skipped + error_count
    ),
    CONSTRAINT ck_dataset_import_operations_errors_count CHECK (
        pg_catalog.jsonb_array_length(errors) <= error_count
    ),
    CONSTRAINT ck_dataset_import_operations_errors_shape CHECK (
        pg_catalog.jsonb_typeof(errors) = 'array'
        AND pg_catalog.jsonb_array_length(errors) <= 100
    ),
    CONSTRAINT ck_dataset_import_operations_lease_pair CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_dataset_import_operations_terminal_lease CHECK (
        stage NOT IN ('completed', 'failed', 'cancelled') OR lease_token IS NULL
    ),
    CONSTRAINT ck_dataset_import_operations_enumerating CHECK (
        stage <> 'enumerating' OR (cursor = 0 AND total IS NULL)
    ),
    CONSTRAINT ck_dataset_import_operations_importing CHECK (
        stage <> 'importing' OR total IS NOT NULL
    ),
    CONSTRAINT ck_dataset_import_operations_completed CHECK (
        stage <> 'completed' OR (total IS NOT NULL AND cursor = total)
    )
);
-- aap-statement-break
REVOKE ALL ON public.dataset_import_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_import_operations IS
    'Assets-owned retained acceptance, connector snapshot, lease, and aggregate state for Dataset imports.';
-- aap-statement-break
CREATE INDEX ix_dataset_import_operations_claim
    ON public.dataset_import_operations (created_at, job_id)
    WHERE stage IN ('enumerating', 'importing');
-- aap-statement-break
CREATE INDEX ix_dataset_import_operations_dataset_stage
    ON public.dataset_import_operations (dataset_id, stage, created_at);
-- aap-statement-break
CREATE TABLE public.dataset_import_entries (
    job_id uuid NOT NULL,
    entry_index integer NOT NULL,
    item_id uuid NOT NULL,
    source jsonb NOT NULL,
    file_name varchar(500),
    file_type varchar(20) NOT NULL,
    publication_id uuid,
    file_path varchar(1000),
    status varchar(16) NOT NULL DEFAULT 'pending',
    file_size bigint,
    content_hash varchar(32),
    width integer,
    height integer,
    error_code varchar(64),
    linked_tasks integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    settled_at timestamptz,
    PRIMARY KEY (job_id, entry_index),
    CONSTRAINT uq_dataset_import_entries_item UNIQUE (job_id, item_id),
    CONSTRAINT uq_dataset_import_entries_publication UNIQUE (job_id, publication_id),
    CONSTRAINT ck_dataset_import_entries_index CHECK (entry_index >= 0),
    CONSTRAINT ck_dataset_import_entries_source_object CHECK (
        pg_catalog.jsonb_typeof(source) = 'object'
    ),
    CONSTRAINT ck_dataset_import_entries_file_type CHECK (
        file_type IN ('image', 'video', 'point_cloud', 'other')
    ),
    CONSTRAINT ck_dataset_import_entries_status CHECK (
        status IN ('pending', 'added', 'skipped', 'error')
    ),
    CONSTRAINT ck_dataset_import_entries_file_size CHECK (
        file_size IS NULL OR file_size >= 0
    ),
    CONSTRAINT ck_dataset_import_entries_content_hash CHECK (
        content_hash IS NULL OR content_hash ~ '^[0-9a-f]{32}$'
    ),
    CONSTRAINT ck_dataset_import_entries_width CHECK (width IS NULL OR width > 0),
    CONSTRAINT ck_dataset_import_entries_height CHECK (height IS NULL OR height > 0),
    CONSTRAINT ck_dataset_import_entries_error_code CHECK (
        error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    CONSTRAINT ck_dataset_import_entries_linked_tasks CHECK (linked_tasks >= 0),
    CONSTRAINT ck_dataset_import_entries_publication_pair CHECK (
        (publication_id IS NULL) = (file_path IS NULL)
    ),
    CONSTRAINT ck_dataset_import_entries_dimension_pair CHECK (
        (width IS NULL) = (height IS NULL)
    ),
    CONSTRAINT ck_dataset_import_entries_pending_result CHECK (
        status <> 'pending' OR (file_size IS NULL AND content_hash IS NULL AND error_code IS NULL AND settled_at IS NULL)
    ),
    CONSTRAINT ck_dataset_import_entries_error_result CHECK (
        status <> 'error' OR (error_code IS NOT NULL AND settled_at IS NOT NULL)
    ),
    CONSTRAINT ck_dataset_import_entries_success_result CHECK (
        status NOT IN ('added', 'skipped') OR (file_size IS NOT NULL AND content_hash IS NOT NULL AND settled_at IS NOT NULL)
    )
);
-- aap-statement-break
REVOKE ALL ON public.dataset_import_entries FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_import_entries IS
    'Assets-owned immutable source manifest and per-entry settlement receipts without live foreign keys.';
-- aap-statement-break
CREATE INDEX ix_dataset_import_entries_item ON public.dataset_import_entries (item_id);
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-import/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-import/v1',
        'ok', true,
        'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_state(p_job_id uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id', operation.job_id,
        'actor_id', operation.actor_id,
        'dataset_id', operation.dataset_id,
        'connection_id', operation.connection_id,
        'source_path', operation.request ->> 'source_path',
        'recursive', operation.request -> 'recursive',
        'include_globs', operation.request -> 'include_globs',
        'settings_snapshot', pg_catalog.jsonb_build_object(
            'dataset_import_max_files', operation.settings_snapshot -> 'dataset_import_max_files',
            'dataset_import_max_total_bytes', operation.settings_snapshot -> 'dataset_import_max_total_bytes',
            'version', operation.settings_snapshot -> 'version'
        ),
        'stage', operation.stage,
        'cursor', operation.cursor,
        'total', operation.total,
        'total_bytes', operation.total_bytes,
        'added', operation.added,
        'skipped', operation.skipped,
        'error_count', operation.error_count,
        'errors', operation.errors,
        'linked_tasks', operation.linked_tasks,
        'committed_bytes', operation.committed_bytes,
        'lease_token', operation.lease_token,
        'lease_expires_at', operation.lease_expires_at
    )
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_state(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_connection_config_valid(
    p_kind text, p_config jsonb
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_key text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_config) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    FOR v_key IN SELECT pg_catalog.jsonb_object_keys(p_config) LOOP
        IF p_kind = 's3' AND v_key NOT IN ('endpoint', 'bucket', 'region', 'base_prefix', 'use_ssl') THEN
            RETURN false;
        ELSIF p_kind = 'sftp' AND v_key NOT IN ('host', 'port', 'username', 'base_path', 'auth_type') THEN
            RETURN false;
        ELSIF p_kind NOT IN ('s3', 'sftp') THEN
            RETURN false;
        END IF;
        IF v_key ~* '(secret|password|credential|token|private_key|access_key)' THEN
            RETURN false;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_each(p_config) AS entry(key, value)
        WHERE pg_catalog.jsonb_typeof(entry.value) = 'string'
          AND (
              pg_catalog.char_length(entry.value #>> '{}') > 2048
              OR entry.value #>> '{}' ~ '[[:cntrl:]]'
          )
    ) THEN
        RETURN false;
    END IF;
    IF p_kind = 's3' THEN
        RETURN p_config ?& ARRAY['endpoint', 'bucket']
           AND pg_catalog.jsonb_typeof(p_config -> 'endpoint') = 'string'
           AND pg_catalog.char_length(p_config ->> 'endpoint') BETWEEN 1 AND 2048
           AND pg_catalog.jsonb_typeof(p_config -> 'bucket') = 'string'
           AND pg_catalog.char_length(p_config ->> 'bucket') BETWEEN 1 AND 255
           AND (NOT (p_config ? 'use_ssl') OR pg_catalog.jsonb_typeof(p_config -> 'use_ssl') = 'boolean')
           AND (NOT (p_config ? 'region') OR (
                pg_catalog.jsonb_typeof(p_config -> 'region') = 'string'
                AND pg_catalog.char_length(p_config ->> 'region') <= 2048
           ))
           AND (NOT (p_config ? 'base_prefix') OR (
                pg_catalog.jsonb_typeof(p_config -> 'base_prefix') = 'string'
                AND pg_catalog.char_length(p_config ->> 'base_prefix') <= 2048
           ))
           AND strpos(p_config ->> 'endpoint', '@') = 0;
    END IF;
    RETURN p_config ?& ARRAY['host', 'username']
       AND pg_catalog.jsonb_typeof(p_config -> 'host') = 'string'
       AND pg_catalog.char_length(p_config ->> 'host') BETWEEN 1 AND 255
       AND pg_catalog.jsonb_typeof(p_config -> 'username') = 'string'
       AND pg_catalog.char_length(p_config ->> 'username') BETWEEN 1 AND 255
       AND (NOT (p_config ? 'port') OR (
            pg_catalog.jsonb_typeof(p_config -> 'port') = 'number'
            AND (p_config ->> 'port') ~ '^[0-9]{1,5}$'
            AND (p_config ->> 'port')::integer BETWEEN 1 AND 65535
       ))
       AND (NOT (p_config ? 'base_path') OR (
            pg_catalog.jsonb_typeof(p_config -> 'base_path') = 'string'
            AND pg_catalog.char_length(p_config ->> 'base_path') <= 2048
       ))
       AND (NOT (p_config ? 'auth_type') OR (
            pg_catalog.jsonb_typeof(p_config -> 'auth_type') = 'string'
            AND pg_catalog.char_length(p_config ->> 'auth_type') <= 2048
       ));
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_connection_config_valid(text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_connection_snapshot(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_import_operations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
BEGIN
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    SELECT connection.* INTO v_connection
    FROM public.storage_connections AS connection
    WHERE connection.id = v_operation.connection_id
    FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_changed');
    END IF;
    IF v_connection.kind IS DISTINCT FROM v_operation.connection_kind
       OR v_connection.name IS DISTINCT FROM v_operation.connection_name
       OR v_connection.scope IS DISTINCT FROM v_operation.connection_scope
       OR v_connection.created_by IS DISTINCT FROM v_operation.connection_created_by
       OR v_connection.config IS DISTINCT FROM v_operation.connection_config
       OR v_connection.secret_enc IS DISTINCT FROM v_operation.connection_secret_enc
       OR v_connection.secret_enc IS NULL
       OR pg_catalog.octet_length(v_connection.secret_enc) NOT BETWEEN 1 AND 49152
       OR pg_catalog.octet_length(v_operation.connection_secret_enc) NOT BETWEEN 1 AND 49152
       OR v_connection.updated_at IS DISTINCT FROM v_operation.connection_updated_at
       OR NOT aap_assets_v1._dataset_import_connection_config_valid(
            v_connection.kind, v_connection.config
       ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_changed');
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        pg_catalog.jsonb_build_object(
            'kind', v_connection.kind,
            'config', v_connection.config,
            'secret_enc', pg_catalog.replace(
                pg_catalog.replace(pg_catalog.encode(v_connection.secret_enc, 'base64'), E'\n', ''),
                E'\r', ''
            )
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_changed');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_connection_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_relative_path_valid(
    p_value text, p_max_characters integer, p_max_bytes integer
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_parts text[];
BEGIN
    IF p_value IS NULL OR p_max_characters IS NULL OR p_max_bytes IS NULL
       OR p_max_characters < 1 OR p_max_bytes < 1
       OR pg_catalog.char_length(p_value) NOT BETWEEN 1 AND p_max_characters
       OR pg_catalog.octet_length(p_value) > p_max_bytes
       OR pg_catalog.left(p_value, 1) = '/'
       OR pg_catalog.strpos(p_value, pg_catalog.chr(92)) > 0
       OR p_value ~ '^[A-Za-z]:/'
       OR p_value ~ '[[:cntrl:]]' THEN
        RETURN false;
    END IF;
    v_parts := pg_catalog.string_to_array(p_value, '/');
    RETURN pg_catalog.array_position(v_parts, '') IS NULL
       AND pg_catalog.array_position(v_parts, '.') IS NULL
       AND pg_catalog.array_position(v_parts, '..') IS NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_relative_path_valid(text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_source_valid(p_source jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_size numeric; v_mtime text; v_etag text;
BEGIN
    IF p_source IS NULL OR pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR (p_source - ARRAY['relpath', 'size', 'mtime', 'etag']) <> '{}'::jsonb
       OR NOT (p_source ?& ARRAY['relpath', 'size', 'mtime', 'etag'])
       OR pg_catalog.jsonb_typeof(p_source -> 'relpath') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_source -> 'size') IS DISTINCT FROM 'number'
       OR pg_catalog.char_length(p_source ->> 'size') > 19
       OR (p_source ->> 'size') !~ '^(0|[1-9][0-9]*)$'
       OR NOT aap_assets_v1._dataset_import_relative_path_valid(
            p_source ->> 'relpath', 4096, 4096
       ) THEN
        RETURN false;
    END IF;
    v_size := (p_source ->> 'size')::numeric;
    IF v_size > 9223372036854775807 THEN
        RETURN false;
    END IF;
    v_mtime := p_source ->> 'mtime';
    IF p_source -> 'mtime' <> 'null'::jsonb
       AND (
            pg_catalog.jsonb_typeof(p_source -> 'mtime') IS DISTINCT FROM 'string'
            OR v_mtime !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$'
       ) THEN
        RETURN false;
    END IF;
    IF p_source -> 'mtime' <> 'null'::jsonb THEN
        PERFORM v_mtime::timestamptz;
    END IF;
    v_etag := p_source ->> 'etag';
    IF p_source -> 'etag' <> 'null'::jsonb
       AND (
            pg_catalog.jsonb_typeof(p_source -> 'etag') IS DISTINCT FROM 'string'
            OR pg_catalog.char_length(v_etag) > 1024
            OR v_etag ~ '[[:cntrl:]]'
       ) THEN
        RETURN false;
    END IF;
    RETURN true;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_source_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_destination_path(
    p_relpath text, p_source_path text, p_base_path text
)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_rel text := pg_catalog.replace(p_relpath, pg_catalog.chr(92), '/');
    v_source text := pg_catalog.btrim(
        pg_catalog.replace(COALESCE(p_source_path, ''), pg_catalog.chr(92), '/'), '/'
    );
    v_base text := pg_catalog.btrim(
        pg_catalog.replace(COALESCE(p_base_path, ''), pg_catalog.chr(92), '/'), '/'
    );
    v_name text;
BEGIN
    IF v_base = '.' THEN v_base := ''; END IF;
    IF v_source = '.' THEN v_source := ''; END IF;
    IF v_base <> '' AND (v_source = v_base OR pg_catalog.left(v_source, pg_catalog.char_length(v_base) + 1) = v_base || '/') THEN
        v_source := pg_catalog.substr(v_source, pg_catalog.char_length(v_base) + 2);
    END IF;
    IF v_source <> '' AND (v_rel = v_source OR pg_catalog.left(v_rel, pg_catalog.char_length(v_source) + 1) = v_source || '/') THEN
        v_rel := pg_catalog.substr(v_rel, pg_catalog.char_length(v_source) + 2);
    END IF;
    IF v_rel = '' THEN
        v_name := pg_catalog.reverse(pg_catalog.split_part(pg_catalog.reverse(p_relpath), '/', 1));
    ELSE
        v_name := v_rel;
    END IF;
    RETURN v_name;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_destination_path(text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_file_type(p_file_name text)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_leaf text := pg_catalog.reverse(pg_catalog.split_part(pg_catalog.reverse(p_file_name), '/', 1));
    v_dot integer;
    v_ext text;
BEGIN
    v_dot := pg_catalog.strpos(pg_catalog.reverse(v_leaf), '.');
    IF v_dot = 0 OR v_dot = pg_catalog.char_length(v_leaf) THEN
        v_ext := '';
    ELSE
        v_ext := pg_catalog.lower(pg_catalog.right(v_leaf, v_dot - 1));
    END IF;
    IF v_ext IN ('jpg', 'jpeg', 'png', 'bmp', 'webp', 'tiff', 'tif', 'gif', 'svg') THEN
        RETURN 'image';
    ELSIF v_ext IN ('mp4', 'avi', 'mov', 'mkv', 'wmv', 'flv', 'webm') THEN
        RETURN 'video';
    ELSIF v_ext IN ('pcd', 'bin', 'las', 'ply') THEN
        RETURN 'point_cloud';
    END IF;
    RETURN 'other';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_file_type(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_actor(p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_actor jsonb;
BEGIN
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor IS NULL OR pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR (v_actor - ARRAY['schema', 'ok', 'data', 'error']) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'account_not_active');
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_actor #>> '{error,code}'
                WHEN 'account_busy' THEN 'account_busy'
                ELSE 'account_not_active'
            END
        );
    END IF;
    IF v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
    END IF;
    IF v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'manager_required');
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'platform_role', v_actor #>> '{data,platform_role}'
        ),
        NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_actor(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_connection_usable(
    p_actor_id uuid, p_platform_role text, p_scope text, p_created_by uuid
)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_platform_role = 'super_admin' THEN true
        WHEN p_scope = 'global' THEN true
        WHEN p_scope = 'owner' THEN COALESCE(p_created_by = p_actor_id, false)
        ELSE false
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_connection_usable(uuid, text, text, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_request_valid(
    p_request jsonb, p_limits jsonb
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_max_files numeric;
    v_max_bytes numeric;
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR (p_request - ARRAY['source_path', 'recursive', 'include_globs']) <> '{}'::jsonb
       OR NOT (p_request ?& ARRAY['source_path', 'recursive', 'include_globs']) THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_request -> 'source_path') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_request ->> 'source_path') > 2048
       OR pg_catalog.octet_length(p_request ->> 'source_path') > 8192
       OR p_request ->> 'source_path' ~ '[[:cntrl:]]'
       OR pg_catalog.strpos('/' || pg_catalog.replace(p_request ->> 'source_path', pg_catalog.chr(92), '/') || '/', '/../') > 0
       OR pg_catalog.jsonb_typeof(p_request -> 'recursive') IS DISTINCT FROM 'boolean' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_request -> 'include_globs') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_array_length(p_request -> 'include_globs') > 256 THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'include_globs') AS item(value)
        WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(item.value #>> '{}') NOT BETWEEN 1 AND 255
           OR item.value #>> '{}' ~ '[[:cntrl:]]'
    ) THEN
        RETURN false;
    END IF;
    IF p_limits IS NULL OR pg_catalog.jsonb_typeof(p_limits) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF (p_limits - ARRAY[
            'dataset_import_max_files', 'dataset_import_max_total_bytes', 'version'
       ]) <> '{}'::jsonb
       OR NOT (p_limits ?& ARRAY[
            'dataset_import_max_files', 'dataset_import_max_total_bytes', 'version'
       ])
       OR pg_catalog.jsonb_typeof(p_limits -> 'dataset_import_max_files') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(p_limits -> 'dataset_import_max_total_bytes') IS DISTINCT FROM 'number'
       OR pg_catalog.char_length(p_limits ->> 'dataset_import_max_files') > 10
       OR pg_catalog.char_length(p_limits ->> 'dataset_import_max_total_bytes') > 19
       OR (p_limits ->> 'dataset_import_max_files') !~ '^[1-9][0-9]*$'
       OR (p_limits ->> 'dataset_import_max_total_bytes') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_limits -> 'version') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_limits ->> 'version') NOT BETWEEN 1 AND 128 THEN
        RETURN false;
    END IF;
    v_max_files := (p_limits ->> 'dataset_import_max_files')::numeric;
    v_max_bytes := (p_limits ->> 'dataset_import_max_total_bytes')::numeric;
    RETURN v_max_files <= 2147483647 AND v_max_bytes <= 9223372036854775807;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_request_valid(jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_job_active(p_job_id uuid, p_dataset_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
BEGIN
    v_reply := aap_jobs_v1.lock_dataset_import_job(p_job_id);
    IF v_reply IS NULL OR pg_catalog.jsonb_typeof(v_reply) IS DISTINCT FROM 'object'
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-dataset-import-job/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF pg_catalog.jsonb_typeof(v_reply -> 'error') = 'object'
           AND v_reply #>> '{error,code}' IN (
                'job_missing', 'operation_conflict', 'dataset_busy', 'invalid_request'
           ) THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, v_reply #>> '{error,code}');
        END IF;
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR v_data ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    IF v_data ->> 'status' = 'cancelled'
       OR v_data -> 'cancel_requested' = 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'import_cancelled');
    END IF;
    IF v_data ->> 'status' NOT IN ('pending', 'running') THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_terminal');
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(v_data, NULL);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_job_active(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_dataset_import(
    p_actor_id uuid, p_dataset_id uuid, p_connection_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_reply jsonb;
    v_actor jsonb;
    v_role text;
    v_dataset_id uuid;
    v_display_id text;
    v_dataset_name text;
    v_connection public.storage_connections%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_connection_id IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    v_actor_reply := aap_assets_v1._dataset_import_actor(p_actor_id);
    IF v_actor_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor_reply;
    END IF;
    v_actor := v_actor_reply -> 'data';
    v_role := v_actor ->> 'platform_role';

    SELECT dataset.id, dataset.display_id, dataset.name
    INTO v_dataset_id, v_display_id, v_dataset_name
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;

    SELECT connection.* INTO v_connection
    FROM public.storage_connections AS connection
    WHERE connection.id = p_connection_id
    FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_missing');
    END IF;
    IF NOT aap_assets_v1._dataset_import_connection_usable(
        p_actor_id, v_role, v_connection.scope, v_connection.created_by
    ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_missing');
    END IF;
    IF v_connection.secret_enc IS NULL
       OR pg_catalog.octet_length(v_connection.secret_enc) NOT BETWEEN 1 AND 49152
       OR NOT aap_assets_v1._dataset_import_connection_config_valid(
            v_connection.kind, v_connection.config
       ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_changed');
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'dataset_id', v_dataset_id,
            'dataset_display_id', v_display_id,
            'dataset_name', v_dataset_name,
            'connection_id', v_connection.id,
            'connection_name', v_connection.name,
            'connection_kind', v_connection.kind,
            'connection_config', v_connection.config
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_dataset_import(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.accept_dataset_import(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_connection_id uuid,
    p_request jsonb,
    p_limits jsonb,
    p_audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_reply jsonb;
    v_actor jsonb;
    v_role text;
    v_dataset_id uuid;
    v_display_id text;
    v_dataset_name text;
    v_connection public.storage_connections%ROWTYPE;
    v_saved public.dataset_import_operations%ROWTYPE;
    v_job_reply jsonb;
    v_detail jsonb;
    v_payload jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_connection_id IS NULL
       OR NOT aap_assets_v1._dataset_import_request_valid(p_request, p_limits)
       OR NOT aap_audit_v1.valid_request_context(p_audit_context) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    v_actor_reply := aap_assets_v1._dataset_import_actor(p_actor_id);
    IF v_actor_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor_reply;
    END IF;
    v_actor := v_actor_reply -> 'data';
    v_role := v_actor ->> 'platform_role';

    -- The parent row serializes acceptance with source deletion and item writers.
    SELECT dataset.id, dataset.display_id, dataset.name
    INTO v_dataset_id, v_display_id, v_dataset_name
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;

    SELECT connection.* INTO v_connection
    FROM public.storage_connections AS connection
    WHERE connection.id = p_connection_id
    FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_missing');
    END IF;
    IF NOT aap_assets_v1._dataset_import_connection_usable(
        p_actor_id, v_role, v_connection.scope, v_connection.created_by
    ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_missing');
    END IF;
    IF v_connection.secret_enc IS NULL
       OR pg_catalog.octet_length(v_connection.secret_enc) NOT BETWEEN 1 AND 49152
       OR NOT aap_assets_v1._dataset_import_connection_config_valid(
            v_connection.kind, v_connection.config
       ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'connection_changed');
    END IF;

    SELECT operation.* INTO v_saved
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_operation_id
    FOR UPDATE NOWAIT;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.dataset_id, v_saved.connection_id, v_saved.request)
           IS DISTINCT FROM (p_actor_id, p_dataset_id, p_connection_id, p_request) THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        v_job_reply := aap_jobs_v1.lock_dataset_import_job(p_operation_id);
        IF v_job_reply IS NULL OR v_job_reply ->> 'schema' IS DISTINCT FROM 'aap-dataset-import-job/v1'
           OR v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_job_reply #>> '{data,dataset_id}' IS DISTINCT FROM p_dataset_id::text THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_import_reply(
            pg_catalog.jsonb_build_object(
                'job_id', p_operation_id,
                'actor_id', v_saved.actor_id,
                'dataset_id', v_saved.dataset_id,
                'connection_id', v_saved.connection_id
            ),
            NULL
        );
    END IF;

    v_payload := pg_catalog.jsonb_build_object(
        'import_protocol', 'aap-dataset-import/v1',
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'dataset_display_id', v_display_id,
        'dataset_name', v_dataset_name,
        'connection_id', v_connection.id,
        'connection_name', v_connection.name,
        'connection_kind', v_connection.kind,
        'source_path', p_request ->> 'source_path',
        'recursive', p_request -> 'recursive',
        'include_globs', p_request -> 'include_globs',
        'settings_snapshot', p_limits
    );
    v_detail := pg_catalog.jsonb_build_object(
        'connection_id', v_connection.id,
        'connection_kind', v_connection.kind,
        'source_path', p_request ->> 'source_path',
        'recursive', p_request -> 'recursive',
        'include_globs', p_request -> 'include_globs',
        'settings_version', p_limits ->> 'version'
    );

    BEGIN
        INSERT INTO public.dataset_import_operations (
            job_id, actor_id, dataset_id, connection_id,
            connection_name, connection_kind, connection_scope,
            connection_created_by, connection_updated_at,
            connection_config, connection_secret_enc,
            request, settings_snapshot, stage
        ) VALUES (
            p_operation_id, p_actor_id, p_dataset_id, p_connection_id,
            v_connection.name, v_connection.kind, v_connection.scope,
            v_connection.created_by, v_connection.updated_at,
            v_connection.config, v_connection.secret_enc,
            p_request, p_limits, 'enumerating'
        );
        v_job_reply := aap_jobs_v1.create_dataset_import_job(
            p_operation_id, p_actor_id, v_payload
        );
        IF v_job_reply IS NULL
           OR v_job_reply ->> 'schema' IS DISTINCT FROM 'aap-dataset-import-job/v1'
           OR v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_job_reply #>> '{data,job_id}' IS DISTINCT FROM p_operation_id::text THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = COALESCE(v_job_reply #>> '{error,code}', 'invalid_response');
        END IF;
        PERFORM aap_audit_v1.record_dataset_import(
            p_operation_id, p_actor_id, p_dataset_id, v_detail, p_audit_context
        );
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RETURN aap_assets_v1._dataset_import_reply(
                NULL,
                CASE SQLERRM
                    WHEN 'account_not_active' THEN 'account_not_active'
                    WHEN 'account_busy' THEN 'account_busy'
                    WHEN 'dataset_busy' THEN 'dataset_busy'
                    WHEN 'operation_conflict' THEN 'operation_conflict'
                    ELSE 'invalid_response'
                END
            );
        WHEN unique_violation THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END;

    RETURN aap_assets_v1._dataset_import_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'connection_id', p_connection_id
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.accept_dataset_import(uuid, uuid, uuid, uuid, jsonb, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_dataset_import(
    p_lease_token uuid,
    p_lease_seconds integer,
    p_requested_job uuid DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_candidate record;
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_job_reply jsonb;
    v_code text;
BEGIN
    IF p_lease_token IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    FOR v_candidate IN
        SELECT operation.job_id, operation.dataset_id
        FROM public.dataset_import_operations AS operation
        WHERE operation.stage IN ('enumerating', 'importing')
          AND (operation.lease_expires_at IS NULL
               OR operation.lease_expires_at <= pg_catalog.clock_timestamp())
          AND (p_requested_job IS NULL OR operation.job_id = p_requested_job)
        ORDER BY operation.created_at, operation.job_id
    LOOP
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-dataset-import:' || v_candidate.job_id::text, 0)
        ) THEN
            CONTINUE;
        END IF;
        BEGIN
            SELECT dataset.id INTO v_dataset_id
            FROM public.datasets AS dataset
            WHERE dataset.id = v_candidate.dataset_id
            FOR NO KEY UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            CONTINUE;
        END;
        IF v_dataset_id IS NULL THEN
            CONTINUE;
        END IF;
        SELECT operation.* INTO v_operation
        FROM public.dataset_import_operations AS operation
        WHERE operation.job_id = v_candidate.job_id
          AND operation.stage IN ('enumerating', 'importing')
          AND (operation.lease_expires_at IS NULL
               OR operation.lease_expires_at <= pg_catalog.clock_timestamp())
        FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        v_job_reply := aap_jobs_v1.claim_dataset_import_job(v_operation.job_id);
        IF v_job_reply IS NULL
           OR v_job_reply ->> 'schema' IS DISTINCT FROM 'aap-dataset-import-job/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs claim contract mismatch';
        END IF;
        IF v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_job_reply #>> '{error,code}';
            IF v_code = 'dataset_busy' THEN
                CONTINUE;
            ELSIF v_code = 'import_cancelled' THEN
                UPDATE public.dataset_import_operations AS operation
                SET stage = 'cancelled', lease_token = NULL, lease_expires_at = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE operation.job_id = v_operation.job_id;
                CONTINUE;
            ELSIF v_code = 'job_terminal' THEN
                UPDATE public.dataset_import_operations AS operation
                SET stage = 'failed', lease_token = NULL, lease_expires_at = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE operation.job_id = v_operation.job_id;
                CONTINUE;
            ELSE
                RETURN aap_assets_v1._dataset_import_reply(NULL, COALESCE(v_code, 'invalid_response'));
            END IF;
        END IF;
        UPDATE public.dataset_import_operations AS operation
        SET lease_token = p_lease_token,
            lease_expires_at = pg_catalog.clock_timestamp()
                + pg_catalog.make_interval(secs => p_lease_seconds),
            updated_at = pg_catalog.clock_timestamp()
        WHERE operation.job_id = v_operation.job_id;
        RETURN aap_assets_v1._dataset_import_reply(
            aap_assets_v1._dataset_import_state(v_operation.job_id), NULL
        );
    END LOOP;
    RETURN aap_assets_v1._dataset_import_reply(NULL, NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_dataset_import(uuid, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.heartbeat_dataset_import(
    p_job_id uuid, p_lease_token uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_active jsonb;
BEGIN
    IF p_job_id IS NULL OR p_lease_token IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    IF v_operation.stage IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_operation.stage WHEN 'cancelled' THEN 'import_cancelled' ELSE 'job_terminal' END
        );
    END IF;
    IF v_operation.lease_token IS DISTINCT FROM p_lease_token
       OR v_operation.lease_expires_at IS NULL
       OR v_operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    v_active := aap_assets_v1._dataset_import_job_active(p_job_id, v_dataset_id);
    IF v_active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_active;
    END IF;
    UPDATE public.dataset_import_operations AS operation
    SET lease_expires_at = pg_catalog.clock_timestamp()
            + pg_catalog.make_interval(secs => p_lease_seconds),
        updated_at = pg_catalog.clock_timestamp()
    WHERE operation.job_id = p_job_id;
    RETURN aap_assets_v1._dataset_import_reply(
        aap_assets_v1._dataset_import_state(p_job_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_dataset_import(uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_dataset_import_connection(
    p_job_id uuid, p_lease_token uuid, p_fallback_allowlist jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_active jsonb;
    v_connection jsonb;
    v_allowlist jsonb;
    v_allowlist_data jsonb;
BEGIN
    IF p_job_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    IF v_operation.stage IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_operation.stage WHEN 'cancelled' THEN 'import_cancelled' ELSE 'job_terminal' END
        );
    END IF;
    IF v_operation.lease_token IS DISTINCT FROM p_lease_token
       OR v_operation.lease_expires_at IS NULL
       OR v_operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    v_active := aap_assets_v1._dataset_import_job_active(p_job_id, v_dataset_id);
    IF v_active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_active;
    END IF;
    v_connection := aap_assets_v1._dataset_import_connection_snapshot(p_job_id);
    IF v_connection -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL, COALESCE(v_connection #>> '{error,code}', 'invalid_response')
        );
    END IF;
    v_allowlist := aap_settings_v1.resolve_connector_host_allowlist(p_fallback_allowlist);
    IF v_allowlist IS NULL
       OR v_allowlist ->> 'schema' IS DISTINCT FROM 'aap-settings-connector-allowlist/v1'
       OR v_allowlist -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_allowlist #>> '{error,code}'
                WHEN 'invalid_request' THEN 'invalid_request'
                ELSE 'invalid_response'
            END
        );
    END IF;
    v_allowlist_data := v_allowlist -> 'data';
    RETURN aap_assets_v1._dataset_import_reply(
        (v_connection -> 'data') || pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'lease_token', p_lease_token,
            'allowlist', v_allowlist_data -> 'entries'
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_dataset_import_connection(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.save_dataset_import_manifest(
    p_job_id uuid, p_lease_token uuid, p_objects jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_active jsonb;
    v_connection jsonb;
    v_total integer;
    v_total_bytes numeric;
    v_existing jsonb;
    v_row record;
    v_max_files integer;
    v_max_bytes bigint;
    v_progress integer;
    v_progress_reply jsonb;
BEGIN
    IF p_job_id IS NULL OR p_lease_token IS NULL OR p_objects IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(p_objects) IS DISTINCT FROM 'array'
       OR pg_catalog.octet_length(p_objects::text) > 16777216 THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    IF v_operation.stage IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_operation.stage WHEN 'cancelled' THEN 'import_cancelled' ELSE 'job_terminal' END
        );
    END IF;
    IF v_operation.lease_token IS DISTINCT FROM p_lease_token
       OR v_operation.lease_expires_at IS NULL
       OR v_operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    v_active := aap_assets_v1._dataset_import_job_active(p_job_id, v_dataset_id);
    IF v_active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_active;
    END IF;
    v_connection := aap_assets_v1._dataset_import_connection_snapshot(p_job_id);
    IF v_connection -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL, COALESCE(v_connection #>> '{error,code}', 'invalid_response')
        );
    END IF;
    v_max_files := (v_operation.settings_snapshot ->> 'dataset_import_max_files')::integer;
    v_max_bytes := (v_operation.settings_snapshot ->> 'dataset_import_max_total_bytes')::bigint;
    v_total := pg_catalog.jsonb_array_length(p_objects);
    IF v_total > v_max_files THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_objects) AS item(value)
        WHERE NOT aap_assets_v1._dataset_import_source_valid(item.value)
    ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    IF (
        SELECT pg_catalog.count(DISTINCT item.value ->> 'relpath')
        FROM pg_catalog.jsonb_array_elements(p_objects) AS item(value)
    ) <> v_total THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT COALESCE(pg_catalog.sum((item.value ->> 'size')::numeric), 0)
    INTO v_total_bytes
    FROM pg_catalog.jsonb_array_elements(p_objects) AS item(value);
    IF v_total_bytes > v_max_bytes THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;

    IF v_operation.total IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.jsonb_agg(entry.source ORDER BY entry.entry_index),
            '[]'::jsonb
        ) INTO v_existing
        FROM public.dataset_import_entries AS entry
        WHERE entry.job_id = p_job_id;
        IF v_operation.total = v_total
           AND v_operation.total_bytes = v_total_bytes
           AND v_existing = p_objects THEN
            RETURN aap_assets_v1._dataset_import_reply(
                aap_assets_v1._dataset_import_state(p_job_id), NULL
            );
        END IF;
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.stage <> 'enumerating' OR v_operation.cursor <> 0 THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;

    FOR v_row IN
        SELECT item.value, item.ordinality
        FROM pg_catalog.jsonb_array_elements(p_objects) WITH ORDINALITY AS item(value, ordinality)
        ORDER BY item.ordinality
    LOOP
        INSERT INTO public.dataset_import_entries (
            job_id, entry_index, item_id, source, file_type
        ) VALUES (
            p_job_id,
            (v_row.ordinality - 1)::integer,
            pg_catalog.gen_random_uuid(),
            v_row.value,
            aap_assets_v1._dataset_import_file_type(v_row.value ->> 'relpath')
        );
    END LOOP;
    UPDATE public.dataset_import_operations AS operation
    SET stage = 'importing', total = v_total, total_bytes = v_total_bytes::bigint,
        updated_at = pg_catalog.clock_timestamp()
    WHERE operation.job_id = p_job_id;
    v_progress := CASE WHEN v_total = 0 THEN 90 ELSE 5 END;
    v_progress_reply := aap_jobs_v1.update_dataset_import_job_progress(
        p_job_id, 'importing', v_progress, v_total, v_total_bytes::bigint
    );
    IF v_progress_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_progress_reply #>> '{data,job_id}' IS DISTINCT FROM p_job_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs progress contract mismatch';
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        aap_assets_v1._dataset_import_state(p_job_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.save_dataset_import_manifest(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.next_dataset_import_entry(
    p_job_id uuid, p_lease_token uuid, p_publication_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_entry public.dataset_import_entries%ROWTYPE;
    v_active jsonb;
    v_connection jsonb;
    v_config jsonb;
    v_base text;
    v_desired text;
    v_candidate text;
    v_leaf text;
    v_dot integer;
    v_stem text;
    v_extension text;
    v_suffix integer := 1;
    v_file_path text;
    v_file_type text;
BEGIN
    IF p_job_id IS NULL OR p_lease_token IS NULL OR p_publication_id IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    IF v_operation.stage IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_operation.stage WHEN 'cancelled' THEN 'import_cancelled' ELSE 'job_terminal' END
        );
    END IF;
    IF v_operation.stage <> 'importing' OR v_operation.total IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.lease_token IS DISTINCT FROM p_lease_token
       OR v_operation.lease_expires_at IS NULL
       OR v_operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    v_active := aap_assets_v1._dataset_import_job_active(p_job_id, v_dataset_id);
    IF v_active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_active;
    END IF;
    IF v_operation.cursor >= v_operation.total THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, NULL);
    END IF;
    v_connection := aap_assets_v1._dataset_import_connection_snapshot(p_job_id);
    IF v_connection -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL, COALESCE(v_connection #>> '{error,code}', 'invalid_response')
        );
    END IF;
    v_config := v_connection #> '{data,config}';
    v_base := CASE v_operation.connection_kind
        WHEN 's3' THEN COALESCE(v_config ->> 'base_prefix', '')
        WHEN 'sftp' THEN COALESCE(v_config ->> 'base_path', '')
        ELSE ''
    END;
    SELECT entry.* INTO v_entry
    FROM public.dataset_import_entries AS entry
    WHERE entry.job_id = p_job_id AND entry.entry_index = v_operation.cursor
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_entry.status <> 'pending' THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM public.dataset_import_entries AS other_entry
        WHERE other_entry.job_id = p_job_id
          AND other_entry.publication_id = p_publication_id
          AND other_entry.entry_index <> v_operation.cursor
    ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;

    v_desired := aap_assets_v1._dataset_import_destination_path(
        v_entry.source ->> 'relpath',
        v_operation.request ->> 'source_path',
        v_base
    );
    v_candidate := v_desired;
    IF v_entry.file_name IS NOT NULL THEN
        v_candidate := v_entry.file_name;
    END IF;

    IF aap_assets_v1._dataset_import_relative_path_valid(v_candidate, 500, 897)
       AND (
            EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE item.dataset_id = v_dataset_id AND item.file_name = v_candidate
            ) OR EXISTS (
                SELECT 1
                FROM public.dataset_import_entries AS other_entry
                JOIN public.dataset_import_operations AS other_operation
                  ON other_operation.job_id = other_entry.job_id
                WHERE other_operation.dataset_id = v_dataset_id
                  AND other_operation.stage IN ('enumerating', 'importing')
                  AND other_entry.file_name = v_candidate
                  AND (other_entry.job_id, other_entry.entry_index)
                      IS DISTINCT FROM (p_job_id, v_operation.cursor)
            )
       ) THEN
        v_leaf := pg_catalog.reverse(
            pg_catalog.split_part(pg_catalog.reverse(v_desired), '/', 1)
        );
        v_dot := pg_catalog.strpos(pg_catalog.reverse(v_leaf), '.');
        IF v_dot > 0 AND v_dot < pg_catalog.char_length(v_leaf) THEN
            v_extension := pg_catalog.right(v_desired, v_dot);
            v_stem := pg_catalog.left(v_desired, pg_catalog.char_length(v_desired) - v_dot);
        ELSE
            v_extension := '';
            v_stem := v_desired;
        END IF;
        LOOP
            v_candidate := v_stem || '-' || v_suffix::text || v_extension;
            EXIT WHEN NOT aap_assets_v1._dataset_import_relative_path_valid(
                v_candidate, 500, 897
            );
            EXIT WHEN NOT EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE item.dataset_id = v_dataset_id AND item.file_name = v_candidate
            ) AND NOT EXISTS (
                SELECT 1
                FROM public.dataset_import_entries AS other_entry
                JOIN public.dataset_import_operations AS other_operation
                  ON other_operation.job_id = other_entry.job_id
                WHERE other_operation.dataset_id = v_dataset_id
                  AND other_operation.stage IN ('enumerating', 'importing')
                  AND other_entry.file_name = v_candidate
                  AND (other_entry.job_id, other_entry.entry_index)
                      IS DISTINCT FROM (p_job_id, v_operation.cursor)
            );
            IF v_suffix = 2147483647 THEN
                RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
            END IF;
            v_suffix := v_suffix + 1;
        END LOOP;
    END IF;
    IF NOT aap_assets_v1._dataset_import_relative_path_valid(v_candidate, 500, 897) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_source_object');
    END IF;
    v_file_path := 'dataset-sources/' || v_dataset_id::text || '/'
        || v_entry.item_id::text || '/' || p_publication_id::text || '/' || v_candidate;
    IF pg_catalog.char_length(v_file_path) > 1000
       OR pg_catalog.octet_length(v_file_path) > 1024 THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_source_object');
    END IF;
    v_file_type := aap_assets_v1._dataset_import_file_type(v_candidate);

    UPDATE public.dataset_import_entries AS entry
    SET file_name = v_candidate,
        file_type = v_file_type,
        publication_id = p_publication_id,
        file_path = v_file_path
    WHERE entry.job_id = p_job_id AND entry.entry_index = v_operation.cursor;
    RETURN aap_assets_v1._dataset_import_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'lease_token', p_lease_token,
            'index', v_operation.cursor,
            'item_id', v_entry.item_id,
            'publication_id', p_publication_id,
            'file_name', v_candidate,
            'file_path', v_file_path,
            'file_type', v_file_type,
            'source', v_entry.source
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.next_dataset_import_entry(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_dataset_import_entry(
    p_job_id uuid,
    p_lease_token uuid,
    p_index bigint,
    p_publication_id uuid,
    p_completion jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_entry public.dataset_import_entries%ROWTYPE;
    v_active jsonb;
    v_connection jsonb;
    v_status text;
    v_file_size bigint;
    v_content_hash text;
    v_width integer;
    v_height integer;
    v_width_json jsonb;
    v_height_json jsonb;
    v_error_code text;
    v_duplicate uuid;
    v_link_reply jsonb;
    v_linked integer := 0;
    v_next_cursor integer;
    v_progress integer;
    v_error_text text;
    v_max_bytes bigint;
BEGIN
    IF p_job_id IS NULL OR p_lease_token IS NULL OR p_index IS NULL
       OR p_index < 0 OR p_index > 2147483647 OR p_publication_id IS NULL
       OR p_completion IS NULL OR pg_catalog.jsonb_typeof(p_completion) IS DISTINCT FROM 'object'
       OR (p_completion - ARRAY[
            'status', 'file_size', 'content_hash', 'width', 'height', 'error_code'
       ]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_completion -> 'status') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    v_status := p_completion ->> 'status';
    IF v_status NOT IN ('copied', 'error') THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    IF v_status = 'copied' THEN
        IF pg_catalog.jsonb_typeof(p_completion -> 'file_size') IS DISTINCT FROM 'number'
           OR pg_catalog.char_length(p_completion ->> 'file_size') > 19
           OR (p_completion ->> 'file_size') !~ '^(0|[1-9][0-9]*)$'
           OR (p_completion ->> 'file_size')::numeric > 9223372036854775807
           OR pg_catalog.jsonb_typeof(p_completion -> 'content_hash') IS DISTINCT FROM 'string'
           OR p_completion ->> 'content_hash' !~ '^[0-9a-f]{32}$'
           OR (p_completion ? 'error_code' AND p_completion -> 'error_code' <> 'null'::jsonb)
           THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
        END IF;
        v_width_json := COALESCE(p_completion -> 'width', 'null'::jsonb);
        v_height_json := COALESCE(p_completion -> 'height', 'null'::jsonb);
        IF (v_width_json = 'null'::jsonb) <> (v_height_json = 'null'::jsonb) THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
        END IF;
        IF v_width_json <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_width_json) IS DISTINCT FROM 'number'
               OR pg_catalog.char_length(v_width_json #>> '{}') > 10
               OR (v_width_json #>> '{}') !~ '^[1-9][0-9]*$'
               OR (v_width_json #>> '{}')::numeric > 2147483647
               OR pg_catalog.jsonb_typeof(v_height_json) IS DISTINCT FROM 'number'
               OR pg_catalog.char_length(v_height_json #>> '{}') > 10
               OR (v_height_json #>> '{}') !~ '^[1-9][0-9]*$'
               OR (v_height_json #>> '{}')::numeric > 2147483647 THEN
                RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
            END IF;
            v_width := (v_width_json #>> '{}')::integer;
            v_height := (v_height_json #>> '{}')::integer;
        END IF;
        v_file_size := (p_completion ->> 'file_size')::bigint;
        v_content_hash := p_completion ->> 'content_hash';
    ELSE
        v_error_code := p_completion ->> 'error_code';
        IF v_error_code IS NULL OR v_error_code !~ '^[a-z][a-z0-9_]{0,63}$'
           OR (p_completion ? 'file_size' AND p_completion -> 'file_size' <> 'null'::jsonb)
           OR (p_completion ? 'content_hash' AND p_completion -> 'content_hash' <> 'null'::jsonb)
           OR (p_completion ? 'width' AND p_completion -> 'width' <> 'null'::jsonb)
           OR (p_completion ? 'height' AND p_completion -> 'height' <> 'null'::jsonb) THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
        END IF;
    END IF;

    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    SELECT entry.* INTO v_entry
    FROM public.dataset_import_entries AS entry
    WHERE entry.job_id = p_job_id AND entry.entry_index = p_index
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    IF p_index < v_operation.cursor
       AND v_entry.status IN ('added', 'skipped', 'error')
       AND (
            v_entry.publication_id = p_publication_id
            OR (
                v_status = 'error'
                AND v_error_code = 'invalid_source_object'
                AND v_entry.status = 'error'
                AND v_entry.error_code = 'invalid_source_object'
                AND v_entry.publication_id IS NULL
                AND v_entry.file_name IS NULL
                AND v_entry.file_path IS NULL
            )
       ) THEN
        RETURN aap_assets_v1._dataset_import_reply(
            aap_assets_v1._dataset_import_state(p_job_id), NULL
        );
    END IF;
    IF v_operation.stage IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_operation.stage WHEN 'cancelled' THEN 'import_cancelled' ELSE 'job_terminal' END
        );
    END IF;
    IF v_operation.stage <> 'importing'
       OR p_index <> v_operation.cursor
       OR v_entry.status <> 'pending' THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    IF v_status = 'error'
       AND v_error_code = 'invalid_source_object'
       AND v_entry.publication_id IS NULL
       AND v_entry.file_name IS NULL
       AND v_entry.file_path IS NULL THEN
        NULL;
    ELSE
        IF v_entry.publication_id IS DISTINCT FROM p_publication_id
           OR v_entry.file_name IS NULL OR v_entry.file_path IS NULL THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        v_expected_file_path := 'dataset-sources/' || v_dataset_id::text || '/'
            || v_entry.item_id::text || '/' || p_publication_id::text || '/' || v_entry.file_name;
        IF v_entry.file_path IS DISTINCT FROM v_expected_file_path
           OR pg_catalog.char_length(v_entry.file_path) > 1000
           OR pg_catalog.octet_length(v_entry.file_path) > 1024 THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
        END IF;
    END IF;
    IF v_operation.lease_token IS DISTINCT FROM p_lease_token
       OR v_operation.lease_expires_at IS NULL
       OR v_operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    v_active := aap_assets_v1._dataset_import_job_active(p_job_id, v_dataset_id);
    IF v_active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_active;
    END IF;
    v_connection := aap_assets_v1._dataset_import_connection_snapshot(p_job_id);
    IF v_connection -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL, COALESCE(v_connection #>> '{error,code}', 'invalid_response')
        );
    END IF;

    v_next_cursor := v_operation.cursor + 1;
    IF v_status = 'error' THEN
        v_error_text := pg_catalog.left(
            (v_entry.source ->> 'relpath') || ': ' || v_error_code,
            512
        );
        UPDATE public.dataset_import_entries AS entry
        SET status = 'error', error_code = v_error_code,
            settled_at = pg_catalog.clock_timestamp()
        WHERE entry.job_id = p_job_id AND entry.entry_index = p_index;
        UPDATE public.dataset_import_operations AS operation
        SET cursor = v_next_cursor,
            error_count = operation.error_count + 1,
            errors = CASE WHEN pg_catalog.jsonb_array_length(operation.errors) < 100
                THEN operation.errors || pg_catalog.jsonb_build_array(v_error_text)
                ELSE operation.errors END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE operation.job_id = p_job_id;
    ELSE
        IF v_file_size IS DISTINCT FROM (v_entry.source ->> 'size')::bigint THEN
            v_error_code := 'source_changed';
            v_error_text := pg_catalog.left(
                (v_entry.source ->> 'relpath') || ': ' || v_error_code,
                512
            );
            UPDATE public.dataset_import_entries AS entry
            SET status = 'error', error_code = v_error_code,
                settled_at = pg_catalog.clock_timestamp()
            WHERE entry.job_id = p_job_id AND entry.entry_index = p_index;
            UPDATE public.dataset_import_operations AS operation
            SET cursor = v_next_cursor,
                error_count = operation.error_count + 1,
                errors = CASE WHEN pg_catalog.jsonb_array_length(operation.errors) < 100
                    THEN operation.errors || pg_catalog.jsonb_build_array(v_error_text)
                    ELSE operation.errors END,
                updated_at = pg_catalog.clock_timestamp()
            WHERE operation.job_id = p_job_id;
        ELSE
            v_max_bytes := (v_operation.settings_snapshot ->> 'dataset_import_max_total_bytes')::bigint;
            IF v_file_size > v_max_bytes - v_operation.committed_bytes THEN
                v_error_code := 'source_changed';
                v_error_text := pg_catalog.left(
                    (v_entry.source ->> 'relpath') || ': ' || v_error_code,
                    512
                );
                UPDATE public.dataset_import_entries AS entry
                SET status = 'error', error_code = v_error_code,
                    settled_at = pg_catalog.clock_timestamp()
                WHERE entry.job_id = p_job_id AND entry.entry_index = p_index;
                UPDATE public.dataset_import_operations AS operation
                SET cursor = v_next_cursor,
                    error_count = operation.error_count + 1,
                    errors = CASE WHEN pg_catalog.jsonb_array_length(operation.errors) < 100
                        THEN operation.errors || pg_catalog.jsonb_build_array(v_error_text)
                        ELSE operation.errors END,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE operation.job_id = p_job_id;
            ELSE
                SELECT item.id INTO v_duplicate
                FROM public.dataset_items AS item
                WHERE item.dataset_id = v_dataset_id
                  AND item.content_hash = v_content_hash
                ORDER BY item.id
                LIMIT 1;
                IF FOUND THEN
                    UPDATE public.dataset_import_entries AS entry
                    SET status = 'skipped', file_size = v_file_size,
                        content_hash = v_content_hash, width = v_width, height = v_height,
                        settled_at = pg_catalog.clock_timestamp()
                    WHERE entry.job_id = p_job_id AND entry.entry_index = p_index;
                    UPDATE public.dataset_import_operations AS operation
                    SET cursor = v_next_cursor,
                        skipped = operation.skipped + 1,
                        committed_bytes = operation.committed_bytes + v_file_size,
                        updated_at = pg_catalog.clock_timestamp()
                    WHERE operation.job_id = p_job_id;
                ELSE
                    IF EXISTS (
                        SELECT 1 FROM public.dataset_items AS item
                        WHERE item.dataset_id = v_dataset_id
                          AND item.file_name = v_entry.file_name
                    ) THEN
                        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
                    END IF;
                    UPDATE public.datasets AS dataset
                    SET file_count = COALESCE(dataset.file_count, 0) + 1,
                        updated_at = pg_catalog.clock_timestamp()
                    WHERE dataset.id = v_dataset_id;
                    IF NOT FOUND THEN
                        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
                    END IF;
                    INSERT INTO public.dataset_items (
                        id, dataset_id, file_name, file_path, file_type, file_size,
                        content_hash, width, height, metadata, created_at, updated_at
                    ) VALUES (
                        v_entry.item_id, v_dataset_id, v_entry.file_name, v_entry.file_path,
                        v_entry.file_type, v_file_size, v_content_hash, v_width, v_height,
                        '{}'::jsonb, pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp()
                    );
                    v_link_reply := aap_labeling_v1.append_dataset_items(
                        v_dataset_id, ARRAY[v_entry.item_id]::uuid[], v_entry.item_id
                    );
                    IF pg_catalog.jsonb_typeof(v_link_reply) IS DISTINCT FROM 'object'
                       OR (v_link_reply - ARRAY[
                            'schema', 'operation_id', 'dataset_id', 'created',
                            'project_totals', 'associations'
                       ]) <> '{}'::jsonb
                       OR NOT (v_link_reply ?& ARRAY[
                            'schema', 'operation_id', 'dataset_id', 'created',
                            'project_totals', 'associations'
                       ])
                       OR v_link_reply ->> 'schema' IS DISTINCT FROM 'aap-task-append/v1'
                       OR v_link_reply ->> 'operation_id' IS DISTINCT FROM v_entry.item_id::text
                       OR v_link_reply ->> 'dataset_id' IS DISTINCT FROM v_dataset_id::text
                       OR pg_catalog.jsonb_typeof(v_link_reply -> 'created') IS DISTINCT FROM 'number'
                       OR (v_link_reply ->> 'created') !~ '^(0|[1-9][0-9]*)$'
                       OR (v_link_reply ->> 'created')::numeric > 2147483647
                       OR pg_catalog.jsonb_typeof(v_link_reply -> 'project_totals') IS DISTINCT FROM 'array'
                       OR pg_catalog.jsonb_typeof(v_link_reply -> 'associations') IS DISTINCT FROM 'array'
                       OR EXISTS (
                            SELECT 1
                            FROM pg_catalog.jsonb_array_elements(v_link_reply -> 'project_totals') AS entry(value)
                            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
                               OR (entry.value - ARRAY['project_id', 'total_tasks']) <> '{}'::jsonb
                               OR NOT (entry.value ?& ARRAY['project_id', 'total_tasks'])
                               OR pg_catalog.jsonb_typeof(entry.value -> 'project_id') IS DISTINCT FROM 'string'
                               OR (entry.value ->> 'project_id') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                               OR pg_catalog.jsonb_typeof(entry.value -> 'total_tasks') IS DISTINCT FROM 'number'
                               OR (entry.value ->> 'total_tasks') !~ '^(0|[1-9][0-9]*)$'
                       )
                       OR EXISTS (
                            SELECT 1
                            FROM pg_catalog.jsonb_array_elements(v_link_reply -> 'associations') AS entry(value)
                            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
                               OR (entry.value - ARRAY['id', 'project_id']) <> '{}'::jsonb
                               OR NOT (entry.value ?& ARRAY['id', 'project_id'])
                               OR pg_catalog.jsonb_typeof(entry.value -> 'id') IS DISTINCT FROM 'string'
                               OR (entry.value ->> 'id') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                               OR pg_catalog.jsonb_typeof(entry.value -> 'project_id') IS DISTINCT FROM 'string'
                               OR (entry.value ->> 'project_id') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                       ) THEN
                        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Labeling append contract mismatch';
                    END IF;
                    v_linked := (v_link_reply ->> 'created')::integer;
                    UPDATE public.dataset_import_entries AS entry
                    SET status = 'added', file_size = v_file_size,
                        content_hash = v_content_hash, width = v_width, height = v_height,
                        linked_tasks = v_linked, settled_at = pg_catalog.clock_timestamp()
                    WHERE entry.job_id = p_job_id AND entry.entry_index = p_index;
                    UPDATE public.dataset_import_operations AS operation
                    SET cursor = v_next_cursor,
                        added = operation.added + 1,
                        linked_tasks = operation.linked_tasks + v_linked,
                        committed_bytes = operation.committed_bytes + v_file_size,
                        updated_at = pg_catalog.clock_timestamp()
                    WHERE operation.job_id = p_job_id;
                    IF v_entry.file_type = 'image' THEN
                        PERFORM aap_assets_v1.queue_dataset_import_delivery(
                            p_job_id, 'thumbnail', v_entry.item_id
                        );
                    ELSIF v_entry.file_type = 'video' THEN
                        PERFORM aap_assets_v1.queue_dataset_import_delivery(
                            p_job_id, 'video_metadata', v_entry.item_id
                        );
                    END IF;
                END IF;
            END IF;
        END IF;
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    v_progress := CASE WHEN v_operation.total = 0 THEN 90
        ELSE 5 + pg_catalog.floor(v_operation.cursor::numeric * 90 / v_operation.total)::integer
    END;
    v_progress_reply := aap_jobs_v1.update_dataset_import_job_progress(
        p_job_id, 'importing', v_progress, v_operation.total, v_operation.total_bytes
    );
    IF v_progress_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_progress_reply #>> '{data,job_id}' IS DISTINCT FROM p_job_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs progress contract mismatch';
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        aap_assets_v1._dataset_import_state(p_job_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_dataset_import_entry(uuid, uuid, bigint, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_result(p_job_id uuid, p_cancelled boolean)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_operation public.dataset_import_operations%ROWTYPE;
BEGIN
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'total', COALESCE(v_operation.total, 0),
        'added', v_operation.added,
        'imported', v_operation.added,
        'skipped', v_operation.skipped + CASE
            WHEN p_cancelled AND v_operation.total IS NOT NULL
            THEN v_operation.total - v_operation.cursor
            ELSE 0
        END,
        'error_count', v_operation.error_count,
        'errors', v_operation.errors,
        'linked_tasks', v_operation.linked_tasks
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_result(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.cancel_dataset_import(p_actor_id uuid, p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_actor_reply jsonb;
    v_job_reply jsonb;
    v_result jsonb;
    v_notification_id uuid;
BEGIN
    IF p_actor_id IS NULL OR p_job_id IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    v_actor_reply := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor_reply IS NULL OR pg_catalog.jsonb_typeof(v_actor_reply) IS DISTINCT FROM 'object'
       OR v_actor_reply ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR (v_actor_reply - ARRAY['schema', 'ok', 'data', 'error']) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
    END IF;
    IF v_actor_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL,
            CASE v_actor_reply #>> '{error,code}'
                WHEN 'account_busy' THEN 'account_busy'
                ELSE 'account_not_active'
            END
        );
    END IF;
    IF v_actor_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor_reply #> '{data,platform_role}') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
    END IF;
    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    v_result := aap_assets_v1._dataset_import_result(p_job_id, true);
    v_job_reply := aap_jobs_v1.cancel_dataset_import_job(
        p_actor_id, p_job_id, v_result
    );
    IF v_job_reply IS NULL
       OR v_job_reply ->> 'schema' IS DISTINCT FROM 'aap-dataset-import-job/v1' THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_response');
    END IF;
    IF v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._dataset_import_reply(
            NULL, COALESCE(v_job_reply #>> '{error,code}', 'invalid_response')
        );
    END IF;
    v_notification_id := NULLIF(v_job_reply #>> '{data,notification_id}', '')::uuid;
    UPDATE public.dataset_import_operations AS operation
    SET stage = 'cancelled',
        lease_token = NULL,
        lease_expires_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE operation.job_id = p_job_id
      AND operation.stage IN ('enumerating', 'importing');
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_terminal');
    END IF;
    IF v_notification_id IS NOT NULL THEN
        PERFORM aap_assets_v1.queue_dataset_import_delivery(
            p_job_id, 'notification', v_notification_id
        );
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        aap_assets_v1._dataset_import_state(p_job_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.cancel_dataset_import(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_dataset_import(
    p_job_id uuid, p_lease_token uuid, p_fatal_error text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_active jsonb;
    v_connection jsonb;
    v_result jsonb;
    v_job_reply jsonb;
    v_notification_id uuid;
    v_error_code text := p_fatal_error;
    v_is_temporal boolean;
BEGIN
    IF p_job_id IS NULL OR p_lease_token IS NULL
       OR (p_fatal_error IS NOT NULL AND p_fatal_error !~ '^[a-z][a-z0-9_]{0,63}$') THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.dataset_id INTO v_dataset_id
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    PERFORM dataset.id FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id
    FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_import_operations AS operation
    WHERE operation.job_id = p_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    IF v_operation.stage IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_assets_v1._dataset_import_reply(
            aap_assets_v1._dataset_import_state(p_job_id), NULL
        );
    END IF;
    IF v_operation.lease_token IS DISTINCT FROM p_lease_token
       OR v_operation.lease_expires_at IS NULL
       OR v_operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    v_active := aap_assets_v1._dataset_import_job_active(p_job_id, v_dataset_id);
    IF v_active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_active;
    END IF;
    v_connection := aap_assets_v1._dataset_import_connection_snapshot(p_job_id);
    IF v_connection -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error_code := COALESCE(v_connection #>> '{error,code}', 'connection_changed');
    END IF;
    IF v_error_code IS NULL AND (
        v_operation.total IS NULL OR v_operation.total_bytes IS NULL
        OR v_operation.cursor <> v_operation.total
    ) THEN
        v_error_code := 'manifest_incomplete';
    END IF;
    IF v_error_code IS NULL THEN
        SELECT dataset.is_temporal INTO v_is_temporal
        FROM public.datasets AS dataset
        WHERE dataset.id = v_dataset_id;
        IF v_is_temporal IS NULL THEN
            RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_missing');
        END IF;
        IF v_is_temporal AND NOT EXISTS (
            SELECT 1 FROM public.scenes AS scene WHERE scene.dataset_id = v_dataset_id
        ) THEN
            v_error_code := 'temporal_dataset_has_no_scenes';
        END IF;
    END IF;
    v_result := aap_assets_v1._dataset_import_result(p_job_id, false);
    IF v_result IS NULL THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'job_missing');
    END IF;
    UPDATE public.dataset_import_operations AS operation
    SET stage = CASE WHEN v_error_code IS NULL THEN 'completed' ELSE 'failed' END,
        lease_token = NULL,
        lease_expires_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE operation.job_id = p_job_id;
    v_job_reply := aap_jobs_v1.finish_dataset_import_job(
        p_job_id,
        CASE WHEN v_error_code IS NULL THEN 'completed' ELSE 'failed' END,
        v_result,
        v_error_code
    );
    IF v_job_reply IS NULL
       OR v_job_reply ->> 'schema' IS DISTINCT FROM 'aap-dataset-import-job/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs finish contract mismatch';
    END IF;
    IF v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error_code := COALESCE(v_job_reply #>> '{error,code}', 'invalid_response');
        IF v_error_code = 'import_cancelled' THEN
            UPDATE public.dataset_import_operations AS operation
            SET stage = 'cancelled', updated_at = pg_catalog.clock_timestamp()
            WHERE operation.job_id = p_job_id;
            RETURN aap_assets_v1._dataset_import_reply(
                aap_assets_v1._dataset_import_state(p_job_id), NULL
            );
        END IF;
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = v_error_code;
    END IF;
    v_notification_id := NULLIF(v_job_reply #>> '{data,notification_id}', '')::uuid;
    IF v_notification_id IS NOT NULL THEN
        PERFORM aap_assets_v1.queue_dataset_import_delivery(
            p_job_id, 'notification', v_notification_id
        );
    END IF;
    RETURN aap_assets_v1._dataset_import_reply(
        aap_assets_v1._dataset_import_state(p_job_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_dataset_import(uuid, uuid, text) FROM PUBLIC;
