-- A bulk manifest freezes logical membership before any accepted bytes are copied.
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-input-manifest/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-input-manifest/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_selection_valid(
    p_selection jsonb
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_previous text;
    v_current text;
    v_item jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY['batch_id', 'task_ids', 'predict_mode'])
            IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_selection ?& ARRAY['batch_id', 'task_ids', 'predict_mode'])
       OR pg_catalog.jsonb_typeof(p_selection -> 'batch_id') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(p_selection -> 'task_ids') NOT IN ('array', 'null')
       OR pg_catalog.jsonb_typeof(p_selection -> 'predict_mode') IS DISTINCT FROM 'string'
       OR p_selection ->> 'predict_mode' NOT IN ('skip_predicted', 'overwrite', 'append') THEN
        RETURN false;
    END IF;
    IF p_selection -> 'batch_id' <> 'null'::jsonb
       AND (p_selection ->> 'batch_id') !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN false;
    END IF;
    IF p_selection -> 'task_ids' = 'null'::jsonb THEN
        RETURN true;
    END IF;
    IF pg_catalog.jsonb_array_length(p_selection -> 'task_ids') NOT BETWEEN 1 AND 200 THEN
        RETURN false;
    END IF;
    FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids')
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'string' THEN
            RETURN false;
        END IF;
        v_current := v_item #>> '{}';
        IF v_current !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (v_previous IS NOT NULL AND v_current <= v_previous) THEN
            RETURN false;
        END IF;
        v_previous := v_current;
    END LOOP;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_selection_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_path_valid(
    p_path text
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_path IS NULL OR pg_catalog.char_length(p_path) NOT BETWEEN 1 AND 1024
       OR p_path LIKE '/%' OR pg_catalog.strpos(p_path, pg_catalog.chr(92)) > 0
       OR pg_catalog.array_position(pg_catalog.string_to_array(p_path, '/'), '') IS NOT NULL THEN
        RETURN false;
    END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM pg_catalog.unnest(pg_catalog.string_to_array(p_path, '/')) AS part(value)
        WHERE part.value IN ('.', '..')
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_path_valid(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_image_input_manifests (
    job_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    selection jsonb NOT NULL,
    selection_digest varchar(64) NOT NULL,
    candidate_count bigint NOT NULL,
    selected_count bigint NOT NULL,
    prepared_count bigint NOT NULL DEFAULT 0,
    status varchar(16) NOT NULL DEFAULT 'preparing',
    ready_count bigint,
    failed_count bigint,
    sealed_digest varchar(64),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    sealed_at timestamptz,
    retire_after timestamptz,
    CONSTRAINT ck_asset_accepted_image_input_manifest_selection CHECK (
        aap_assets_v1._accepted_image_input_manifest_selection_valid(selection)
    ),
    CONSTRAINT ck_asset_accepted_image_input_manifest_identity CHECK (
        selection_digest ~ '^[0-9a-f]{64}$'
        AND candidate_count >= 0
        AND selected_count >= 0
        AND selected_count <= candidate_count
        AND prepared_count >= 0
        AND prepared_count <= selected_count
    ),
    CONSTRAINT ck_asset_accepted_image_input_manifest_barrier CHECK (
        (status = 'preparing'
            AND ready_count IS NULL
            AND failed_count IS NULL
            AND sealed_digest IS NULL
            AND sealed_at IS NULL)
        OR (status = 'sealed'
            AND prepared_count = selected_count
            AND ready_count IS NOT NULL
            AND failed_count IS NOT NULL
            AND ready_count >= 0
            AND failed_count >= 0
            AND ready_count + failed_count = selected_count
            AND sealed_digest ~ '^[0-9a-f]{64}$'
            AND sealed_at IS NOT NULL
            AND sealed_at >= created_at)
    )
);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_accepted_image_input_manifests FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_image_input_manifest_members (
    job_id uuid NOT NULL,
    ordinal bigint NOT NULL,
    task_id uuid NOT NULL,
    project_id uuid NOT NULL,
    batch_id uuid,
    task_version integer NOT NULL,
    task_source_generation bigint NOT NULL,
    dataset_item_id uuid,
    task_source_snapshot varchar(64) NOT NULL,
    file_path varchar(1024) NOT NULL,
    file_type varchar(20) NOT NULL,
    source jsonb,
    state varchar(16) NOT NULL,
    input_id uuid,
    error_code varchar(64),
    outcome_byte_size bigint,
    outcome_sha256 varchar(64),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (job_id, ordinal),
    CONSTRAINT uq_asset_accepted_image_input_manifest_task UNIQUE (job_id, task_id),
    CONSTRAINT uq_asset_accepted_image_input_manifest_input UNIQUE (input_id),
    CONSTRAINT ck_asset_accepted_image_input_manifest_member_identity CHECK (
        ordinal > 0
        AND task_version > 0
        AND task_source_generation > 0
        AND task_source_snapshot ~ '^[0-9a-f]{64}$'
        AND aap_assets_v1._accepted_image_input_manifest_path_valid(file_path)
        AND file_type = 'image'
        AND (source IS NULL OR (
            aap_assets_v1._image_source_data_valid(source)
            AND source ->> 'task_id' = task_id::text
            AND source ->> 'project_id' = project_id::text
        ))
    ),
    CONSTRAINT ck_asset_accepted_image_input_manifest_member_state CHECK (
        state IN ('unprepared', 'preparing', 'ready', 'failed')
        AND (
            (state = 'unprepared' AND source IS NOT NULL
                AND input_id IS NULL AND error_code IS NULL
                AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
            OR (state = 'preparing' AND source IS NOT NULL
                AND input_id IS NOT NULL AND error_code IS NULL
                AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
            OR (state = 'ready' AND source IS NOT NULL
                AND input_id IS NOT NULL AND error_code IS NULL
                AND outcome_byte_size IS NOT NULL AND outcome_byte_size >= 0
                AND outcome_sha256 IS NOT NULL
                AND outcome_sha256 ~ '^[0-9a-f]{64}$')
            OR (state = 'failed' AND error_code IS NOT NULL
                AND (input_id IS NULL OR source IS NOT NULL)
                AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
        )
    )
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_image_input_manifest_members_task
    ON public.asset_accepted_image_input_manifest_members(task_id, job_id);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_image_input_manifest_members_input
    ON public.asset_accepted_image_input_manifest_members(input_id)
    WHERE input_id IS NOT NULL;
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_accepted_image_input_manifest_members FROM PUBLIC;
