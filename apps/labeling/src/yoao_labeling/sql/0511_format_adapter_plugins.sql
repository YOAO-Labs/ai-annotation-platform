CREATE TABLE yoao_labeling_v1.format_adapter_plugins (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    format_id text NOT NULL UNIQUE
        CHECK (
            format_id ~ '^[a-z][a-z0-9_-]{1,63}$'
            AND format_id NOT IN (
                'native_json', 'coco', 'yolo', 'yolo-det', 'yolo-obb', 'yolo-seg',
                'label-studio-brush', 'binary-png', 'indexed-png', 'video_json',
                'yolo-frames-det', 'yolo-frames-seg', 'coco-frames-seg', 'davis',
                'youtube-vos', 'mots', 'mot', 'kitti', 'nuscenes',
                'coco-multicamera', 'pointmask', 'voc'
            )
        ),
    display_name text NOT NULL
        CHECK (pg_catalog.char_length(pg_catalog.btrim(display_name)) BETWEEN 1 AND 64),
    description text
        CHECK (
            description IS NULL
            OR pg_catalog.char_length(pg_catalog.btrim(description)) BETWEEN 1 AND 280
        ),
    homepage text
        CHECK (homepage IS NULL OR pg_catalog.char_length(homepage) <= 255),
    source_tier text NOT NULL DEFAULT 'third_party'
        CHECK (source_tier IN ('official', 'third_party')),
    mode text NOT NULL CHECK (mode IN ('direct', 'managed')),
    status text NOT NULL CHECK (status IN ('prepared', 'active', 'disabled', 'retired')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    endpoint_url text NOT NULL,
    auth_token_ciphertext text,
    auth_token_sha256 text NOT NULL,
    protocol_version text NOT NULL,
    adapter_version text NOT NULL,
    manifest_version text NOT NULL DEFAULT '1',
    media_types jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(media_types) = 'array'),
    capabilities jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(capabilities) = 'array'),
    option_schema jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (pg_catalog.jsonb_typeof(option_schema) = 'object'),
    manifest jsonb CHECK (manifest IS NULL OR pg_catalog.jsonb_typeof(manifest) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    disabled_at timestamptz,
    retired_at timestamptz,
    CHECK (
        (status = 'retired' AND auth_token_ciphertext IS NULL)
        OR (status <> 'retired' AND auth_token_ciphertext IS NOT NULL)
    )
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_labeling_v1.format_adapter_plugins FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1._format_adapter_public_projection(
    p_row yoao_labeling_v1.format_adapter_plugins
) RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_row.id,
        'format_id', p_row.format_id,
        'display_name', p_row.display_name,
        'description', p_row.description,
        'homepage', p_row.homepage,
        'source_tier', p_row.source_tier,
        'mode', p_row.mode,
        'status', p_row.status,
        'revision', p_row.revision,
        'endpoint_url', p_row.endpoint_url,
        'protocol_version', p_row.protocol_version,
        'adapter_version', p_row.adapter_version,
        'manifest_version', p_row.manifest_version,
        'media_types', p_row.media_types,
        'capabilities', p_row.capabilities,
        'option_schema', p_row.option_schema,
        'manifest', p_row.manifest,
        'created_at', p_row.created_at,
        'updated_at', p_row.updated_at,
        'disabled_at', p_row.disabled_at,
        'retired_at', p_row.retired_at
    );
$$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1._format_adapter_public_projection(
    yoao_labeling_v1.format_adapter_plugins
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.list_format_adapter_plugins(
    p_include_retired boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, yoao_labeling_v1
AS $$
DECLARE
    v_items jsonb;
BEGIN
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            yoao_labeling_v1._format_adapter_public_projection(f)
            ORDER BY f.format_id
        ),
        '[]'::jsonb
    )
    INTO v_items
    FROM yoao_labeling_v1.format_adapter_plugins f
    WHERE p_include_retired OR f.status <> 'retired';
    RETURN v_items;
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.get_format_adapter_plugin(
    p_format_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, yoao_labeling_v1
AS $$
DECLARE
    v_row yoao_labeling_v1.format_adapter_plugins%ROWTYPE;
BEGIN
    SELECT * INTO v_row
    FROM yoao_labeling_v1.format_adapter_plugins
    WHERE format_id = p_format_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN yoao_labeling_v1._format_adapter_public_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.register_format_adapter_plugin(
    p_input jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, yoao_labeling_v1
AS $$
DECLARE
    v_format_id text := p_input->>'format_id';
    v_status text := COALESCE(p_input->>'status', 'active');
    v_row yoao_labeling_v1.format_adapter_plugins%ROWTYPE;
BEGIN
    IF v_format_id IS NULL OR v_format_id = '' THEN
        RAISE EXCEPTION 'format_id is required' USING ERRCODE = '22023';
    END IF;
    IF v_status NOT IN ('prepared', 'active') THEN
        RAISE EXCEPTION 'initial status must be prepared or active' USING ERRCODE = '22023';
    END IF;

    INSERT INTO yoao_labeling_v1.format_adapter_plugins (
        format_id,
        display_name,
        description,
        homepage,
        source_tier,
        mode,
        status,
        revision,
        endpoint_url,
        auth_token_ciphertext,
        auth_token_sha256,
        protocol_version,
        adapter_version,
        manifest_version,
        media_types,
        capabilities,
        option_schema,
        manifest
    ) VALUES (
        v_format_id,
        p_input->>'display_name',
        p_input->>'description',
        p_input->>'homepage',
        COALESCE(p_input->>'source_tier', 'third_party'),
        COALESCE(p_input->>'mode', 'direct'),
        v_status,
        1,
        p_input->>'endpoint_url',
        p_input->>'auth_token_ciphertext',
        p_input->>'auth_token_sha256',
        p_input->>'protocol_version',
        p_input->>'adapter_version',
        COALESCE(p_input->>'manifest_version', '1'),
        p_input->'media_types',
        p_input->'capabilities',
        COALESCE(p_input->'option_schema', '{}'::jsonb),
        CASE
            WHEN p_input ? 'manifest' AND pg_catalog.jsonb_typeof(p_input->'manifest') <> 'null'
            THEN p_input->'manifest'
            ELSE NULL
        END
    )
    RETURNING * INTO v_row;

    RETURN yoao_labeling_v1._format_adapter_public_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.transition_format_adapter_plugin(
    p_input jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, yoao_labeling_v1
AS $$
DECLARE
    v_format_id text := p_input->>'format_id';
    v_expected_revision integer := (p_input->>'expected_revision')::integer;
    v_target_status text := p_input->>'target_status';
    v_row yoao_labeling_v1.format_adapter_plugins%ROWTYPE;
    v_now timestamptz := pg_catalog.now();
BEGIN
    SELECT * INTO v_row
    FROM yoao_labeling_v1.format_adapter_plugins
    WHERE format_id = v_format_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'format_adapter_not_found' USING ERRCODE = 'P0002';
    END IF;
    IF v_expected_revision IS NULL THEN
        RAISE EXCEPTION 'expected_revision is required' USING ERRCODE = '22023';
    END IF;
    IF v_row.revision <> v_expected_revision THEN
        RAISE EXCEPTION 'format_adapter_revision_conflict' USING ERRCODE = '40001';
    END IF;
    IF v_row.status = 'retired' THEN
        RAISE EXCEPTION 'format_adapter_already_retired' USING ERRCODE = '22023';
    END IF;
    IF v_target_status NOT IN ('active', 'disabled', 'retired') THEN
        RAISE EXCEPTION 'invalid target_status' USING ERRCODE = '22023';
    END IF;
    IF v_row.status = v_target_status THEN
        RAISE EXCEPTION 'format_adapter_status_unchanged' USING ERRCODE = '22023';
    END IF;

    UPDATE yoao_labeling_v1.format_adapter_plugins
    SET
        status = v_target_status,
        revision = v_row.revision + 1,
        display_name = COALESCE(p_input->>'display_name', v_row.display_name),
        description = CASE WHEN p_input ? 'description' THEN p_input->>'description' ELSE v_row.description END,
        homepage = CASE WHEN p_input ? 'homepage' THEN p_input->>'homepage' ELSE v_row.homepage END,
        adapter_version = COALESCE(p_input->>'adapter_version', v_row.adapter_version),
        manifest_version = COALESCE(p_input->>'manifest_version', v_row.manifest_version),
        media_types = COALESCE(p_input->'media_types', v_row.media_types),
        capabilities = COALESCE(p_input->'capabilities', v_row.capabilities),
        option_schema = COALESCE(p_input->'option_schema', v_row.option_schema),
        auth_token_ciphertext = CASE
            WHEN v_target_status = 'retired' THEN NULL
            WHEN p_input ? 'auth_token_ciphertext' THEN p_input->>'auth_token_ciphertext'
            ELSE v_row.auth_token_ciphertext
        END,
        auth_token_sha256 = CASE
            WHEN p_input ? 'auth_token_sha256' THEN p_input->>'auth_token_sha256'
            ELSE v_row.auth_token_sha256
        END,
        disabled_at = CASE
            WHEN v_target_status = 'disabled' THEN v_now
            WHEN v_target_status = 'active' THEN NULL
            ELSE v_row.disabled_at
        END,
        retired_at = CASE
            WHEN v_target_status = 'retired' THEN v_now
            ELSE v_row.retired_at
        END,
        updated_at = v_now
    WHERE id = v_row.id
    RETURNING * INTO v_row;

    RETURN yoao_labeling_v1._format_adapter_public_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.open_format_adapter_plugin_secret(
    p_format_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, yoao_labeling_v1
AS $$
DECLARE
    v_row yoao_labeling_v1.format_adapter_plugins%ROWTYPE;
BEGIN
    SELECT * INTO v_row
    FROM yoao_labeling_v1.format_adapter_plugins
    WHERE format_id = p_format_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'format_id', v_row.format_id,
        'status', v_row.status,
        'endpoint_url', v_row.endpoint_url,
        'auth_token_ciphertext', v_row.auth_token_ciphertext,
        'protocol_version', v_row.protocol_version,
        'adapter_version', v_row.adapter_version,
        'manifest_version', v_row.manifest_version,
        'display_name', v_row.display_name,
        'media_types', v_row.media_types,
        'capabilities', v_row.capabilities,
        'option_schema', v_row.option_schema
    );
END;
$$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.list_format_adapter_plugins(boolean) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.get_format_adapter_plugin(text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.register_format_adapter_plugin(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.transition_format_adapter_plugin(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.open_format_adapter_plugin_secret(text) FROM PUBLIC;
-- aap-statement-break
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'application_role') THEN
        GRANT USAGE ON SCHEMA yoao_labeling_v1 TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_labeling_v1.list_format_adapter_plugins(boolean) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_labeling_v1.get_format_adapter_plugin(text) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_labeling_v1.register_format_adapter_plugin(jsonb) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_labeling_v1.transition_format_adapter_plugin(jsonb) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_labeling_v1.open_format_adapter_plugin_secret(text) TO application_role;
    END IF;
END
$$;
