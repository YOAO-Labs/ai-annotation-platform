CREATE TABLE yoao_learning_v1.trainer_plugins (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    plugin_id text NOT NULL UNIQUE
        CHECK (plugin_id ~ '^[a-z][a-z0-9._-]{1,127}$'),
    version text NOT NULL
        CHECK (pg_catalog.char_length(pg_catalog.btrim(version)) BETWEEN 1 AND 64),
    framework_id text NOT NULL
        CHECK (framework_id ~ '^[a-z][a-z0-9_-]{0,63}$'),
    display_name text NOT NULL
        CHECK (pg_catalog.char_length(pg_catalog.btrim(display_name)) BETWEEN 1 AND 64),
    description text
        CHECK (
            description IS NULL
            OR pg_catalog.char_length(pg_catalog.btrim(description)) BETWEEN 1 AND 280
        ),
    homepage text
        CHECK (homepage IS NULL OR pg_catalog.char_length(homepage) <= 255),
    publisher text
        CHECK (
            publisher IS NULL
            OR pg_catalog.char_length(pg_catalog.btrim(publisher)) BETWEEN 1 AND 128
        ),
    license text
        CHECK (
            license IS NULL
            OR pg_catalog.char_length(pg_catalog.btrim(license)) BETWEEN 1 AND 64
        ),
    source_tier text NOT NULL DEFAULT 'third_party'
        CHECK (source_tier IN ('official', 'third_party')),
    execution_mode text NOT NULL DEFAULT 'container'
        CHECK (execution_mode IN ('container', 'local_process')),
    status text NOT NULL DEFAULT 'registered'
        CHECK (status IN ('registered', 'qualified', 'enabled', 'disabled', 'retired')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    protocol_version text NOT NULL CHECK (protocol_version = 'yoao-trainer/v1'),
    artifact text
        CHECK (
            (
                execution_mode = 'container'
                AND artifact IS NOT NULL
                AND artifact ~ '^[^@\s]+@sha256:[0-9a-f]{64}$'
            )
            OR (execution_mode = 'local_process' AND artifact IS NULL)
        ),
    entrypoint jsonb NOT NULL
        CHECK (
            pg_catalog.jsonb_typeof(entrypoint) = 'array'
            AND pg_catalog.jsonb_array_length(entrypoint) >= 1
        ),
    resource jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (pg_catalog.jsonb_typeof(resource) = 'object'),
    capabilities jsonb NOT NULL
        CHECK (
            pg_catalog.jsonb_typeof(capabilities) = 'array'
            AND pg_catalog.jsonb_array_length(capabilities) >= 1
        ),
    manifest jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(manifest) = 'object'),
    capabilities_snapshot jsonb NOT NULL
        CHECK (pg_catalog.jsonb_typeof(capabilities_snapshot) = 'object'),
    capabilities_digest text NOT NULL CHECK (capabilities_digest ~ '^[0-9a-f]{64}$'),
    qualification_report_sha256 text
        CHECK (
            qualification_report_sha256 IS NULL
            OR qualification_report_sha256 ~ '^[0-9a-f]{64}$'
        ),
    qualification_report jsonb
        CHECK (
            qualification_report IS NULL
            OR pg_catalog.jsonb_typeof(qualification_report) = 'object'
        ),
    qualified_at timestamptz,
    enabled_at timestamptz,
    disabled_at timestamptz,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CHECK (
        (
            status IN ('qualified', 'enabled', 'disabled')
            AND qualification_report_sha256 IS NOT NULL
        )
        OR status IN ('registered', 'retired')
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_trainer_plugins_active_framework
    ON yoao_learning_v1.trainer_plugins (framework_id)
    WHERE status <> 'retired';
-- aap-statement-break
REVOKE ALL ON TABLE yoao_learning_v1.trainer_plugins FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1._trainer_plugin_projection(
    p_row yoao_learning_v1.trainer_plugins
) RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_row.id,
        'plugin_id', p_row.plugin_id,
        'version', p_row.version,
        'extension_point', 'trainer',
        'framework_id', p_row.framework_id,
        'display_name', p_row.display_name,
        'description', p_row.description,
        'homepage', p_row.homepage,
        'publisher', p_row.publisher,
        'license', p_row.license,
        'source_tier', p_row.source_tier,
        'execution_mode', p_row.execution_mode,
        'status', p_row.status,
        'revision', p_row.revision,
        'protocol_version', p_row.protocol_version,
        'artifact', p_row.artifact,
        'entrypoint', p_row.entrypoint,
        'resource', p_row.resource,
        'capabilities', p_row.capabilities,
        'manifest', p_row.manifest,
        'capabilities_snapshot', p_row.capabilities_snapshot,
        'capabilities_digest', p_row.capabilities_digest,
        'qualification_report_sha256', p_row.qualification_report_sha256,
        'qualification_report', p_row.qualification_report,
        'qualified_at', p_row.qualified_at,
        'enabled_at', p_row.enabled_at,
        'disabled_at', p_row.disabled_at,
        'retired_at', p_row.retired_at,
        'created_at', p_row.created_at,
        'updated_at', p_row.updated_at
    );
$$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1._trainer_plugin_projection(
    yoao_learning_v1.trainer_plugins
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.list_trainer_plugins(
    p_include_retired boolean DEFAULT false,
    p_status text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_items jsonb;
BEGIN
    IF p_status IS NOT NULL
       AND p_status NOT IN ('registered', 'qualified', 'enabled', 'disabled', 'retired')
    THEN
        RAISE EXCEPTION 'invalid_trainer_plugin_status' USING ERRCODE = '22023';
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            yoao_learning_v1._trainer_plugin_projection(t)
            ORDER BY t.plugin_id
        ),
        '[]'::jsonb
    )
    INTO v_items
    FROM yoao_learning_v1.trainer_plugins t
    WHERE (p_status IS NOT NULL AND t.status = p_status)
       OR (p_status IS NULL AND (p_include_retired OR t.status <> 'retired'));

    RETURN v_items;
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.get_trainer_plugin(
    p_plugin_id text
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_row yoao_learning_v1.trainer_plugins%ROWTYPE;
BEGIN
    SELECT * INTO v_row
    FROM yoao_learning_v1.trainer_plugins
    WHERE plugin_id = p_plugin_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN yoao_learning_v1._trainer_plugin_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.get_enabled_trainer_plugin_by_framework(
    p_framework_id text
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_row yoao_learning_v1.trainer_plugins%ROWTYPE;
BEGIN
    SELECT * INTO v_row
    FROM yoao_learning_v1.trainer_plugins
    WHERE framework_id = p_framework_id
      AND status = 'enabled';
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN yoao_learning_v1._trainer_plugin_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.register_trainer_plugin(
    p_input jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_plugin_id text := p_input->>'plugin_id';
    v_row yoao_learning_v1.trainer_plugins%ROWTYPE;
BEGIN
    IF v_plugin_id IS NULL OR v_plugin_id = '' THEN
        RAISE EXCEPTION 'plugin_id is required' USING ERRCODE = '22023';
    END IF;

    INSERT INTO yoao_learning_v1.trainer_plugins (
        plugin_id,
        version,
        framework_id,
        display_name,
        description,
        homepage,
        publisher,
        license,
        source_tier,
        execution_mode,
        status,
        revision,
        protocol_version,
        artifact,
        entrypoint,
        resource,
        capabilities,
        manifest,
        capabilities_snapshot,
        capabilities_digest
    ) VALUES (
        v_plugin_id,
        p_input->>'version',
        p_input->>'framework_id',
        p_input->>'display_name',
        CASE
            WHEN p_input ? 'description' AND pg_catalog.jsonb_typeof(p_input->'description') <> 'null'
            THEN p_input->>'description'
            ELSE NULL
        END,
        CASE
            WHEN p_input ? 'homepage' AND pg_catalog.jsonb_typeof(p_input->'homepage') <> 'null'
            THEN p_input->>'homepage'
            ELSE NULL
        END,
        CASE
            WHEN p_input ? 'publisher' AND pg_catalog.jsonb_typeof(p_input->'publisher') <> 'null'
            THEN p_input->>'publisher'
            ELSE NULL
        END,
        CASE
            WHEN p_input ? 'license' AND pg_catalog.jsonb_typeof(p_input->'license') <> 'null'
            THEN p_input->>'license'
            ELSE NULL
        END,
        COALESCE(p_input->>'source_tier', 'third_party'),
        COALESCE(p_input->>'execution_mode', 'container'),
        'registered',
        1,
        p_input->>'protocol_version',
        CASE
            WHEN p_input ? 'artifact' AND pg_catalog.jsonb_typeof(p_input->'artifact') <> 'null'
            THEN p_input->>'artifact'
            ELSE NULL
        END,
        p_input->'entrypoint',
        COALESCE(p_input->'resource', '{}'::jsonb),
        p_input->'capabilities',
        p_input->'manifest',
        p_input->'capabilities_snapshot',
        p_input->>'capabilities_digest'
    )
    RETURNING * INTO v_row;

    RETURN yoao_learning_v1._trainer_plugin_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.qualify_trainer_plugin(
    p_input jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_plugin_id text := p_input->>'plugin_id';
    v_expected_revision integer := (p_input->>'expected_revision')::integer;
    v_report_sha256 text := p_input->>'qualification_report_sha256';
    v_report jsonb := p_input->'qualification_report';
    v_row yoao_learning_v1.trainer_plugins%ROWTYPE;
    v_now timestamptz := pg_catalog.now();
BEGIN
    SELECT * INTO v_row
    FROM yoao_learning_v1.trainer_plugins
    WHERE plugin_id = v_plugin_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'trainer_plugin_not_found' USING ERRCODE = 'P0002';
    END IF;
    IF v_expected_revision IS NULL THEN
        RAISE EXCEPTION 'expected_revision is required' USING ERRCODE = '22023';
    END IF;
    IF v_row.revision <> v_expected_revision THEN
        RAISE EXCEPTION 'trainer_plugin_revision_conflict' USING ERRCODE = '40001';
    END IF;
    IF v_row.status NOT IN ('registered', 'qualified', 'disabled') THEN
        RAISE EXCEPTION 'trainer_plugin_cannot_qualify_in_status' USING ERRCODE = '22023';
    END IF;
    IF v_report_sha256 IS NULL
       OR v_report_sha256 !~ '^[0-9a-f]{64}$'
       OR v_report IS NULL
       OR pg_catalog.jsonb_typeof(v_report) <> 'object'
    THEN
        RAISE EXCEPTION 'invalid_qualification_report' USING ERRCODE = '22023';
    END IF;

    UPDATE yoao_learning_v1.trainer_plugins
    SET
        status = CASE WHEN v_row.status = 'registered' THEN 'qualified' ELSE v_row.status END,
        revision = v_row.revision + 1,
        qualification_report_sha256 = v_report_sha256,
        qualification_report = v_report,
        qualified_at = v_now,
        updated_at = v_now
    WHERE id = v_row.id
    RETURNING * INTO v_row;

    RETURN yoao_learning_v1._trainer_plugin_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.transition_trainer_plugin(
    p_input jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_plugin_id text := p_input->>'plugin_id';
    v_expected_revision integer := (p_input->>'expected_revision')::integer;
    v_target_status text := p_input->>'target_status';
    v_row yoao_learning_v1.trainer_plugins%ROWTYPE;
    v_now timestamptz := pg_catalog.now();
BEGIN
    SELECT * INTO v_row
    FROM yoao_learning_v1.trainer_plugins
    WHERE plugin_id = v_plugin_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'trainer_plugin_not_found' USING ERRCODE = 'P0002';
    END IF;
    IF v_expected_revision IS NULL THEN
        RAISE EXCEPTION 'expected_revision is required' USING ERRCODE = '22023';
    END IF;
    IF v_row.revision <> v_expected_revision THEN
        RAISE EXCEPTION 'trainer_plugin_revision_conflict' USING ERRCODE = '40001';
    END IF;
    IF v_row.status = 'retired' THEN
        RAISE EXCEPTION 'trainer_plugin_already_retired' USING ERRCODE = '22023';
    END IF;
    IF v_target_status IS NULL OR v_target_status NOT IN ('enabled', 'disabled', 'retired') THEN
        RAISE EXCEPTION 'invalid_target_status' USING ERRCODE = '22023';
    END IF;
    IF v_row.status = 'registered' AND v_target_status <> 'retired' THEN
        RAISE EXCEPTION 'trainer_plugin_not_qualified' USING ERRCODE = '22023';
    END IF;
    IF v_row.status = 'qualified' AND v_target_status NOT IN ('enabled', 'retired') THEN
        RAISE EXCEPTION 'trainer_plugin_invalid_transition' USING ERRCODE = '22023';
    END IF;
    IF v_row.status = 'enabled' AND v_target_status NOT IN ('disabled', 'retired') THEN
        RAISE EXCEPTION 'trainer_plugin_invalid_transition' USING ERRCODE = '22023';
    END IF;
    IF v_row.status = 'disabled' AND v_target_status NOT IN ('enabled', 'retired') THEN
        RAISE EXCEPTION 'trainer_plugin_invalid_transition' USING ERRCODE = '22023';
    END IF;
    IF v_target_status = 'enabled' AND v_row.qualification_report_sha256 IS NULL THEN
        RAISE EXCEPTION 'trainer_plugin_not_qualified' USING ERRCODE = '22023';
    END IF;

    UPDATE yoao_learning_v1.trainer_plugins
    SET
        status = v_target_status,
        revision = v_row.revision + 1,
        enabled_at = CASE
            WHEN v_target_status = 'enabled' THEN v_now
            ELSE v_row.enabled_at
        END,
        disabled_at = CASE
            WHEN v_target_status = 'disabled' THEN v_now
            WHEN v_target_status = 'enabled' THEN NULL
            ELSE v_row.disabled_at
        END,
        retired_at = CASE
            WHEN v_target_status = 'retired' THEN v_now
            ELSE v_row.retired_at
        END,
        updated_at = v_now
    WHERE id = v_row.id
    RETURNING * INTO v_row;

    RETURN yoao_learning_v1._trainer_plugin_projection(v_row);
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.training_list_gpu_leases()
RETURNS SETOF public.training_resource_leases
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT *
    FROM public.training_resource_leases
    ORDER BY resource_id;
$$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.list_trainer_plugins(boolean, text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.get_trainer_plugin(text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.get_enabled_trainer_plugin_by_framework(text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.register_trainer_plugin(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.qualify_trainer_plugin(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.transition_trainer_plugin(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.training_list_gpu_leases() FROM PUBLIC;
-- aap-statement-break
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'application_role') THEN
        GRANT USAGE ON SCHEMA yoao_learning_v1 TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.list_trainer_plugins(boolean, text) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.get_trainer_plugin(text) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.get_enabled_trainer_plugin_by_framework(text) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.register_trainer_plugin(jsonb) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.qualify_trainer_plugin(jsonb) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.transition_trainer_plugin(jsonb) TO application_role;
        GRANT EXECUTE ON FUNCTION yoao_learning_v1.training_list_gpu_leases() TO application_role;
    END IF;
END
$$;
