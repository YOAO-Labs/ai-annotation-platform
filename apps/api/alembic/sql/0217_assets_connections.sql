-- Source owns connection configuration and immutable connection-operation receipts.
CREATE TABLE public.storage_connection_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    action text NOT NULL CONSTRAINT ck_storage_connection_operations_action
        CHECK (action IN ('create', 'update', 'delete', 'test')),
    connection_id uuid NOT NULL,
    intent jsonb NOT NULL CONSTRAINT ck_storage_connection_operations_intent
        CHECK (pg_catalog.jsonb_typeof(intent) = 'object'),
    connection_scope text NOT NULL CONSTRAINT ck_storage_connection_operations_scope
        CHECK (connection_scope IN ('global', 'owner')),
    connection_created_by uuid,
    result jsonb NOT NULL CONSTRAINT ck_storage_connection_operations_result CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
        AND result ->> 'operation_id' = operation_id::text
        AND result ->> 'actor_id' = actor_id::text
        AND result ->> 'action' = action
        AND result ->> 'connection_id' = connection_id::text
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
REVOKE ALL ON TABLE public.storage_connection_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.storage_connection_operations IS
    'Assets-owned immutable successful storage connection mutations and probe receipts; deliberately has no live-resource foreign keys.';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._storage_connection_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-connections/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-connections/v1', 'ok', true, 'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

-- This authorization helper is deliberately independent of platform role for
-- existing owner connections: a creator keeps management after role changes.
CREATE FUNCTION aap_assets_v1._storage_connection_access(
    p_actor_id uuid,
    p_platform_role text,
    p_scope text,
    p_created_by uuid,
    p_access text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_scope IS NULL OR p_scope NOT IN ('global', 'owner', 'project') THEN 'invalid_response'
        WHEN p_scope = 'global' AND p_access = 'use' THEN NULL
        WHEN p_scope = 'global' AND p_access = 'admin'
             AND p_platform_role <> 'super_admin' THEN 'global_management_forbidden'
        WHEN p_scope = 'global' AND p_access = 'admin' THEN NULL
        WHEN p_scope <> 'global'
             AND (p_platform_role = 'super_admin' OR p_created_by = p_actor_id) THEN NULL
        WHEN p_access = 'use' THEN 'connection_missing'
        ELSE 'connection_management_forbidden'
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_access(uuid, text, text, uuid, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_actor(p_actor_id uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_actor_id uuid;
    v_role text;
    v_active boolean;
BEGIN
    IF p_actor_id IS NULL OR p_lock IS NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    IF p_lock THEN
        v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
        IF v_actor IS NULL
           OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
           OR (v_actor - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_response');
        END IF;
        IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._storage_connection_reply(
                NULL, COALESCE(v_actor #>> '{error,code}', 'account_not_active')
            );
        END IF;
        v_actor_id := (v_actor #>> '{data,actor_id}')::uuid;
        v_role := v_actor #>> '{data,platform_role}';
        IF v_actor_id IS DISTINCT FROM p_actor_id THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_response');
        END IF;
    ELSE
        SELECT facts.actor_id, facts.platform_role, facts.is_active
        INTO v_actor_id, v_role, v_active
        FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
        IF NOT FOUND OR v_active IS DISTINCT FROM true THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'account_not_active');
        END IF;
        IF v_actor_id IS DISTINCT FROM p_actor_id THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_response');
        END IF;
    END IF;
    IF v_role IS NULL OR v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_account_role');
    END IF;
    RETURN aap_assets_v1._storage_connection_reply(
        pg_catalog.jsonb_build_object('actor_id', v_actor_id, 'platform_role', v_role), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_actor(uuid, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_facts(
    p_connection public.storage_connections
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', (p_connection).id,
        'name', (p_connection).name,
        'kind', (p_connection).kind,
        'config', (p_connection).config,
        'scope', (p_connection).scope,
        'project_id', (p_connection).project_id,
        'secret_set', (p_connection).secret_enc IS NOT NULL,
        'created_by', (p_connection).created_by,
        'created_at', (p_connection).created_at,
        'updated_at', (p_connection).updated_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_facts(public.storage_connections) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_snapshot(p_connection public.storage_connections)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'id', (p_connection).id,
                    'name', (p_connection).name,
                    'kind', (p_connection).kind,
                    'config', (p_connection).config,
                    'scope', (p_connection).scope,
                    'project_id', (p_connection).project_id,
                    'secret_enc', CASE WHEN (p_connection).secret_enc IS NULL THEN NULL
                        ELSE pg_catalog.replace(pg_catalog.encode((p_connection).secret_enc, 'base64'), E'\n', '') END,
                    'created_by', (p_connection).created_by,
                    'created_at', (p_connection).created_at,
                    'updated_at', (p_connection).updated_at
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_snapshot(public.storage_connections) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_config_valid(p_kind text, p_config jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_endpoint_authority text;
BEGIN
    IF p_config IS NULL OR pg_catalog.jsonb_typeof(p_config) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF p_kind = 's3' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_config) AS key(name)
            WHERE key.name <> ALL (ARRAY['endpoint', 'bucket', 'region', 'base_prefix', 'use_ssl'])
        ) OR pg_catalog.jsonb_typeof(p_config -> 'endpoint') IS DISTINCT FROM 'string'
          OR pg_catalog.char_length(p_config ->> 'endpoint') = 0
          OR NOT (p_config ? 'bucket') THEN
            RETURN false;
        END IF;
        v_endpoint_authority := CASE
            WHEN pg_catalog.strpos(p_config ->> 'endpoint', '://') > 0 THEN
                pg_catalog.split_part(
                    pg_catalog.split_part((p_config ->> 'endpoint'), '://', 2), '/', 1
                )
            ELSE pg_catalog.split_part(
                    pg_catalog.ltrim((p_config ->> 'endpoint'), '/'), '/', 1
                 )
        END;
        IF pg_catalog.strpos(pg_catalog.split_part(v_endpoint_authority, '?', 1), '@') > 0 THEN
            RETURN false;
        END IF;
        RETURN true;
    ELSIF p_kind = 'sftp' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_config) AS key(name)
            WHERE key.name <> ALL (ARRAY['host', 'port', 'username', 'base_path', 'auth_type'])
        ) OR NOT (p_config ? 'host') OR NOT (p_config ? 'username') THEN
            RETURN false;
        END IF;
        IF p_config ? 'auth_type'
           AND p_config -> 'auth_type' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_config -> 'auth_type') IS DISTINCT FROM 'string'
                OR p_config ->> 'auth_type' NOT IN ('password', 'key')) THEN
            RETURN false;
        END IF;
        RETURN true;
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_config_valid(text, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_intent_valid(p_action text, p_intent jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_intent) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF p_action = 'create' THEN
        RETURN (p_intent - ARRAY['name', 'kind', 'config', 'scope', 'secret_digest']) = '{}'::jsonb
           AND p_intent ?& ARRAY['name', 'kind', 'config', 'scope', 'secret_digest']
           AND pg_catalog.jsonb_typeof(p_intent -> 'name') = 'string'
           AND pg_catalog.char_length(p_intent ->> 'name') BETWEEN 1 AND 255
           AND pg_catalog.jsonb_typeof(p_intent -> 'kind') = 'string'
           AND p_intent ->> 'kind' IN ('s3', 'sftp')
           AND aap_assets_v1._storage_connection_config_valid(p_intent ->> 'kind', p_intent -> 'config')
           AND pg_catalog.jsonb_typeof(p_intent -> 'scope') = 'string'
           AND p_intent ->> 'scope' IN ('global', 'owner')
           AND pg_catalog.jsonb_typeof(p_intent -> 'secret_digest') = 'string'
           AND p_intent ->> 'secret_digest' ~ '^[0-9a-f]{64}$';
    ELSIF p_action = 'update' THEN
        RETURN (p_intent - ARRAY['name', 'config', 'secret_digest']) = '{}'::jsonb
           AND p_intent ?& ARRAY['name', 'config', 'secret_digest']
           AND (pg_catalog.jsonb_typeof(p_intent -> 'name') = 'null'
                OR (pg_catalog.jsonb_typeof(p_intent -> 'name') = 'string'
                    AND pg_catalog.char_length(p_intent ->> 'name') BETWEEN 1 AND 255))
           AND (pg_catalog.jsonb_typeof(p_intent -> 'config') = 'null'
                OR (pg_catalog.jsonb_typeof(p_intent -> 'config') = 'object'
                    AND aap_assets_v1._storage_connection_config_valid(
                        CASE WHEN p_intent -> 'config' ? 'host' THEN 'sftp' ELSE 's3' END,
                        p_intent -> 'config'
                    )))
           AND (pg_catalog.jsonb_typeof(p_intent -> 'secret_digest') = 'null'
                OR (pg_catalog.jsonb_typeof(p_intent -> 'secret_digest') = 'string'
                    AND p_intent ->> 'secret_digest' ~ '^[0-9a-f]{64}$'));
    ELSIF p_action IN ('delete', 'test') THEN
        RETURN p_intent = '{}'::jsonb;
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_intent_valid(text, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_ciphertext_valid(p_secret_enc text)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_secret bytea;
BEGIN
    IF p_secret_enc IS NULL OR p_secret_enc = ''
       OR p_secret_enc !~ '^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$' THEN
        RETURN false;
    END IF;
    v_secret := pg_catalog.decode(p_secret_enc, 'base64');
    RETURN pg_catalog.octet_length(v_secret) BETWEEN 1 AND 49152;
EXCEPTION WHEN invalid_text_representation OR data_exception THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_ciphertext_valid(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_policy(p_fallback jsonb, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_setting jsonb;
    v_source text;
    v_allowlist jsonb;
    v_digest text;
    v_code text;
BEGIN
    IF p_lock IS NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    IF p_lock THEN
        v_setting := aap_settings_v1.lock_connector_host_allowlist(p_fallback);
    ELSE
        v_setting := aap_settings_v1.resolve_connector_host_allowlist(p_fallback);
    END IF;
    IF v_setting IS NULL OR v_setting ->> 'schema' IS DISTINCT FROM 'aap-settings-connector-allowlist/v1'
       OR (v_setting - ARRAY['schema', 'ok', 'data', 'error']) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_unavailable');
    END IF;
    IF v_setting -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_setting #>> '{error,code}';
        IF v_code = 'invalid_request' THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
        ELSIF v_code = 'policy_busy' THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_busy');
        END IF;
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_unavailable');
    END IF;
    IF ((v_setting -> 'data') - ARRAY['entries', 'source']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_setting #> '{data,entries}') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_setting #> '{data,source}') IS DISTINCT FROM 'string'
       OR v_setting #>> '{data,source}' NOT IN ('database', 'environment') THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_unavailable');
    END IF;
    v_allowlist := v_setting #> '{data,entries}';
    v_source := v_setting #>> '{data,source}';
    v_digest := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'allowlist', v_allowlist,
                    'policy_source', v_source
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    RETURN aap_assets_v1._storage_connection_reply(
        pg_catalog.jsonb_build_object(
            'allowlist', v_allowlist,
            'policy_source', v_source,
            'policy_digest', v_digest
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_policy(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._storage_connection_operation_lock(p_operation_id uuid)
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_operation_id IS NOT NULL AND pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-storage-connection:' || p_operation_id::text, 0)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_operation_lock(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.list_storage_connections(
    actor_id uuid,
    after_created_at timestamptz,
    after_id uuid,
    page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_role text;
    v_visible jsonb;
    v_total bigint;
    v_digest text;
    v_connections jsonb := '[]'::jsonb;
    v_next jsonb;
    v_more boolean := false;
    v_seen integer := 0;
    v_page record;
BEGIN
    IF actor_id IS NULL OR page_size IS NULL OR page_size NOT BETWEEN 1 AND 500
       OR (after_created_at IS NULL) <> (after_id IS NULL) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';

    FOR v_page IN
        WITH visible AS MATERIALIZED (
            SELECT connection.id, connection.created_at,
                   aap_assets_v1._storage_connection_facts(connection) AS facts
            FROM public.storage_connections AS connection
            WHERE v_role = 'super_admin'
               OR connection.scope = 'global'
               OR connection.created_by = actor_id
        ), summary AS (
            SELECT count(*) AS total,
                   COALESCE(
                       pg_catalog.jsonb_agg(
                           visible.facts ORDER BY visible.created_at DESC, visible.id DESC
                       ),
                       '[]'::jsonb
                   ) AS visible_rows
            FROM visible
        ), page AS (
            SELECT visible.id, visible.created_at, visible.facts,
                   pg_catalog.row_number() OVER (
                       ORDER BY visible.created_at DESC, visible.id DESC
                   ) AS page_number
            FROM visible
            WHERE after_created_at IS NULL
               OR (visible.created_at, visible.id) < (after_created_at, after_id)
            ORDER BY visible.created_at DESC, visible.id DESC
            LIMIT page_size + 1
        )
        SELECT summary.total, summary.visible_rows,
               page.id, page.created_at, page.facts, page.page_number
        FROM summary
        LEFT JOIN page ON true
        ORDER BY page.page_number
    LOOP
        v_total := v_page.total;
        v_visible := v_page.visible_rows;
        v_digest := pg_catalog.encode(
            public.digest(pg_catalog.convert_to(v_visible::text, 'UTF8'), 'sha256'),
            'hex'
        );
        IF v_page.page_number IS NULL THEN
            EXIT;
        END IF;
        v_seen := v_seen + 1;
        IF v_seen > page_size THEN
            v_more := true;
            EXIT;
        END IF;
        v_connections := v_connections || pg_catalog.jsonb_build_array(v_page.facts);
        v_next := pg_catalog.jsonb_build_object(
            'created_at', v_page.created_at,
            'id', v_page.id
        );
    END LOOP;

    RETURN aap_assets_v1._storage_connection_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', actor_id,
            'after', CASE WHEN after_created_at IS NULL THEN NULL
                ELSE pg_catalog.jsonb_build_object('created_at', after_created_at, 'id', after_id) END,
            'next_cursor', CASE WHEN v_more THEN v_next ELSE NULL END,
            'snapshot_digest', v_digest,
            'total', v_total,
            'complete', NOT v_more,
            'connections', v_connections
        ),
        NULL
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.list_storage_connections(uuid, timestamptz, uuid, integer) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_storage_connection(actor_id uuid, connection_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_row public.storage_connections%ROWTYPE;
    v_code text;
BEGIN
    IF actor_id IS NULL OR connection_id IS NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    SELECT connection.* INTO v_row
    FROM public.storage_connections AS connection
    WHERE connection.id = read_storage_connection.connection_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_missing');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id, v_actor #>> '{data,platform_role}', v_row.scope, v_row.created_by, 'use'
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    RETURN aap_assets_v1._storage_connection_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', actor_id,
            'connection_id', connection_id,
            'connection', aap_assets_v1._storage_connection_facts(v_row)
        ),
        NULL
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_storage_connection(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_storage_connection_policy(actor_id uuid, fallback_allowlist jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_policy jsonb;
BEGIN
    IF actor_id IS NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    v_policy := aap_assets_v1._storage_connection_policy(fallback_allowlist, false);
    IF v_policy -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_policy;
    END IF;
    RETURN aap_assets_v1._storage_connection_reply(
        (v_policy -> 'data') || pg_catalog.jsonb_build_object(
            'actor_id', actor_id,
            'platform_role', v_actor #>> '{data,platform_role}'
        ),
        NULL
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_storage_connection_policy(uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.capture_storage_connection(
    actor_id uuid,
    connection_id uuid,
    access text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_row public.storage_connections%ROWTYPE;
    v_code text;
    v_secret text;
BEGIN
    IF actor_id IS NULL OR connection_id IS NULL OR access IS NULL
       OR access NOT IN ('use', 'admin') THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    SELECT connection.* INTO v_row
    FROM public.storage_connections AS connection
    WHERE connection.id = capture_storage_connection.connection_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_missing');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id, v_actor #>> '{data,platform_role}', v_row.scope, v_row.created_by, access
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    v_secret := CASE WHEN v_row.secret_enc IS NULL THEN NULL
        ELSE pg_catalog.replace(pg_catalog.encode(v_row.secret_enc, 'base64'), E'\n', '') END;
    RETURN aap_assets_v1._storage_connection_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', actor_id,
            'platform_role', v_actor #>> '{data,platform_role}',
            'connection_id', connection_id,
            'access', access,
            'connection', aap_assets_v1._storage_connection_facts(v_row),
            'secret_enc', v_secret,
            'source_snapshot', aap_assets_v1._storage_connection_snapshot(v_row)
        ),
        NULL
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_storage_connection(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_storage_connection_receipt(
    operation_id uuid,
    actor_id uuid,
    action text,
    connection_id uuid,
    intent jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_saved public.storage_connection_operations%ROWTYPE;
    v_code text;
BEGIN
    IF operation_id IS NULL OR actor_id IS NULL OR connection_id IS NULL
       OR action NOT IN ('create', 'update', 'delete', 'test')
       OR NOT aap_assets_v1._storage_connection_intent_valid(action, intent) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    IF action = 'create' AND connection_id IS DISTINCT FROM operation_id THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    SELECT receipt.* INTO v_saved
    FROM public.storage_connection_operations AS receipt
    WHERE receipt.operation_id = read_storage_connection_receipt.operation_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', operation_id, 'actor_id', actor_id,
                'action', action, 'connection_id', connection_id, 'result', NULL
            ),
            NULL
        );
    END IF;
    IF (v_saved.actor_id, v_saved.action, v_saved.connection_id, v_saved.intent)
       IS DISTINCT FROM (actor_id, action, connection_id, intent) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id,
        v_actor #>> '{data,platform_role}',
        v_saved.connection_scope,
        v_saved.connection_created_by,
        CASE WHEN action = 'test' THEN 'use' ELSE 'admin' END
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    RETURN aap_assets_v1._storage_connection_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', operation_id, 'actor_id', actor_id,
            'action', action, 'connection_id', connection_id,
            'result', v_saved.result
        ),
        NULL
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_storage_connection_receipt(uuid, uuid, text, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.create_storage_connection(
    operation_id uuid,
    actor_id uuid,
    intent jsonb,
    config jsonb,
    secret_enc text,
    policy_digest text,
    fallback_allowlist jsonb,
    audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_role text;
    v_saved public.storage_connection_operations%ROWTYPE;
    v_policy jsonb;
    v_row public.storage_connections%ROWTYPE;
    v_secret bytea;
    v_result jsonb;
    v_scope text;
    v_code text;
BEGIN
    IF operation_id IS NULL OR actor_id IS NULL
       OR NOT aap_assets_v1._storage_connection_intent_valid('create', intent)
       OR config IS NULL OR NOT aap_assets_v1._storage_connection_config_valid(intent ->> 'kind', config)
       OR policy_digest IS NULL OR policy_digest !~ '^[0-9a-f]{64}$'
       OR NOT aap_assets_v1._storage_connection_ciphertext_valid(secret_enc)
       OR NOT aap_audit_v1.valid_request_context(audit_context)
       OR (config IS DISTINCT FROM (intent -> 'config')
           AND NOT (
               intent ->> 'kind' = 'sftp'
               AND (NOT ((intent -> 'config') ? 'auth_type')
                    OR (intent -> 'config' -> 'auth_type') = 'null'::jsonb)
               AND config = (intent -> 'config') || pg_catalog.jsonb_build_object('auth_type', config -> 'auth_type')
               AND config ->> 'auth_type' IN ('password', 'key')
           )) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, true);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    v_scope := intent ->> 'scope';
    IF NOT aap_assets_v1._storage_connection_operation_lock(operation_id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END IF;
    SELECT receipt.* INTO v_saved
    FROM public.storage_connection_operations AS receipt
    WHERE receipt.operation_id = operation_id;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.action, v_saved.connection_id, v_saved.intent)
           IS DISTINCT FROM (actor_id, 'create'::text, operation_id, intent) THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
        END IF;
        v_code := aap_assets_v1._storage_connection_access(
            actor_id, v_role, v_saved.connection_scope, v_saved.connection_created_by, 'admin'
        );
        IF v_code IS NOT NULL THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._storage_connection_reply(v_saved.result, NULL);
    END IF;

    IF v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'manager_required');
    END IF;
    IF v_scope = 'global' AND v_role <> 'super_admin' THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'global_creation_forbidden');
    END IF;

    v_policy := aap_assets_v1._storage_connection_policy(fallback_allowlist, true);
    IF v_policy -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_policy;
    END IF;
    IF v_policy #>> '{data,policy_digest}' IS DISTINCT FROM policy_digest THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_changed');
    END IF;

    v_secret := pg_catalog.decode(secret_enc, 'base64');
    INSERT INTO public.storage_connections (
        id, name, kind, config, secret_enc, scope, project_id, created_by
    ) VALUES (
        operation_id, intent ->> 'name', intent ->> 'kind', config, v_secret,
        v_scope, NULL, actor_id
    ) RETURNING * INTO v_row;
    v_result := pg_catalog.jsonb_build_object(
        'operation_id', operation_id, 'actor_id', actor_id,
        'action', 'create', 'connection_id', operation_id,
        'connection', aap_assets_v1._storage_connection_facts(v_row)
    );
    PERFORM aap_audit_v1.record_storage_connection_change(
        operation_id, actor_id, 'create', operation_id, audit_context,
        pg_catalog.jsonb_build_object('scope', v_scope, 'kind', v_row.kind)
    );
    INSERT INTO public.storage_connection_operations (
        operation_id, actor_id, action, connection_id, intent,
        connection_scope, connection_created_by, result
    ) VALUES (
        operation_id, actor_id, 'create', operation_id, intent,
        v_scope, actor_id, v_result
    );
    RETURN aap_assets_v1._storage_connection_reply(v_result, NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.create_storage_connection(uuid, uuid, jsonb, jsonb, text, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.update_storage_connection(
    operation_id uuid,
    actor_id uuid,
    connection_id uuid,
    intent jsonb,
    config jsonb,
    secret_enc text,
    source_snapshot text,
    policy_digest text,
    fallback_allowlist jsonb,
    audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_role text;
    v_saved public.storage_connection_operations%ROWTYPE;
    v_row public.storage_connections%ROWTYPE;
    v_policy jsonb;
    v_secret bytea;
    v_name text;
    v_config jsonb;
    v_result jsonb;
    v_code text;
BEGIN
    IF operation_id IS NULL OR actor_id IS NULL OR connection_id IS NULL
       OR NOT aap_assets_v1._storage_connection_intent_valid('update', intent)
       OR config IS NULL OR pg_catalog.jsonb_typeof(config) IS DISTINCT FROM 'object'
       OR source_snapshot IS NULL OR source_snapshot !~ '^[0-9a-f]{64}$'
       OR (policy_digest IS NOT NULL AND policy_digest !~ '^[0-9a-f]{64}$')
       OR ((intent -> 'config') IS DISTINCT FROM 'null'::jsonb AND policy_digest IS NULL)
       OR ((intent -> 'config') IS DISTINCT FROM 'null'::jsonb
           AND NOT aap_assets_v1._storage_connection_config_valid(
               CASE WHEN intent -> 'config' ? 'host' THEN 'sftp' ELSE 's3' END, config))
       OR ((intent -> 'config') = 'null'::jsonb AND policy_digest IS NOT NULL)
       OR (intent -> 'secret_digest' = 'null'::jsonb AND secret_enc IS NOT NULL)
       OR (intent -> 'secret_digest' <> 'null'::jsonb
           AND NOT aap_assets_v1._storage_connection_ciphertext_valid(secret_enc))
       OR NOT aap_audit_v1.valid_request_context(audit_context) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, true);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF NOT aap_assets_v1._storage_connection_operation_lock(operation_id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END IF;
    SELECT receipt.* INTO v_saved
    FROM public.storage_connection_operations AS receipt
    WHERE receipt.operation_id = operation_id;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.action, v_saved.connection_id, v_saved.intent)
           IS DISTINCT FROM (actor_id, 'update'::text, connection_id, intent) THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
        END IF;
        v_code := aap_assets_v1._storage_connection_access(
            actor_id, v_role, v_saved.connection_scope, v_saved.connection_created_by, 'admin'
        );
        IF v_code IS NOT NULL THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._storage_connection_reply(v_saved.result, NULL);
    END IF;

    BEGIN
        SELECT connection.* INTO v_row
        FROM public.storage_connections AS connection
        WHERE connection.id = connection_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_missing');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id, v_role, v_row.scope, v_row.created_by, 'admin'
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    IF aap_assets_v1._storage_connection_snapshot(v_row) IS DISTINCT FROM source_snapshot THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'source_changed');
    END IF;
    IF (intent -> 'config') = 'null'::jsonb THEN
        IF config IS DISTINCT FROM v_row.config
           AND NOT (
               v_row.kind = 'sftp'
               AND (NOT (v_row.config ? 'auth_type') OR v_row.config -> 'auth_type' = 'null'::jsonb)
               AND intent -> 'secret_digest' <> 'null'::jsonb
               AND config = v_row.config || pg_catalog.jsonb_build_object('auth_type', config -> 'auth_type')
               AND config ->> 'auth_type' IN ('password', 'key')
           ) THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
        END IF;
    ELSIF NOT aap_assets_v1._storage_connection_config_valid(v_row.kind, intent -> 'config')
       OR NOT aap_assets_v1._storage_connection_config_valid(v_row.kind, config) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'validation_failed');
    ELSIF config IS DISTINCT FROM intent -> 'config'
       AND NOT (
            v_row.kind = 'sftp'
            AND (NOT ((intent -> 'config') ? 'auth_type')
                 OR (intent -> 'config' -> 'auth_type') = 'null'::jsonb)
            AND config = (intent -> 'config') || pg_catalog.jsonb_build_object('auth_type', config -> 'auth_type')
            AND config ->> 'auth_type' IN ('password', 'key')
       ) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    IF (intent -> 'config') IS DISTINCT FROM 'null'::jsonb THEN
        v_policy := aap_assets_v1._storage_connection_policy(fallback_allowlist, true);
        IF v_policy -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_policy;
        END IF;
        IF v_policy #>> '{data,policy_digest}' IS DISTINCT FROM policy_digest THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_changed');
        END IF;
    END IF;

    v_name := COALESCE(intent ->> 'name', v_row.name);
    v_config := config;
    v_secret := CASE WHEN secret_enc IS NULL THEN v_row.secret_enc ELSE pg_catalog.decode(secret_enc, 'base64') END;
    IF (v_name, v_config, v_secret) IS DISTINCT FROM (v_row.name, v_row.config, v_row.secret_enc) THEN
        UPDATE public.storage_connections AS connection
        SET name = v_name, config = v_config, secret_enc = v_secret,
            updated_at = pg_catalog.clock_timestamp()
        WHERE connection.id = connection_id
        RETURNING connection.* INTO v_row;
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'operation_id', operation_id, 'actor_id', actor_id,
        'action', 'update', 'connection_id', connection_id,
        'connection', aap_assets_v1._storage_connection_facts(v_row)
    );
    PERFORM aap_audit_v1.record_storage_connection_change(
        operation_id, actor_id, 'update', connection_id, audit_context,
        pg_catalog.jsonb_build_object('scope', CASE WHEN v_row.scope = 'global' THEN 'global' ELSE 'owner' END)
    );
    INSERT INTO public.storage_connection_operations (
        operation_id, actor_id, action, connection_id, intent,
        connection_scope, connection_created_by, result
    ) VALUES (
        operation_id, actor_id, 'update', connection_id, intent,
        CASE WHEN v_row.scope = 'global' THEN 'global' ELSE 'owner' END,
        v_row.created_by, v_result
    );
    RETURN aap_assets_v1._storage_connection_reply(v_result, NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.update_storage_connection(uuid, uuid, uuid, jsonb, jsonb, text, text, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.delete_storage_connection(
    operation_id uuid,
    actor_id uuid,
    connection_id uuid,
    audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_role text;
    v_saved public.storage_connection_operations%ROWTYPE;
    v_row public.storage_connections%ROWTYPE;
    v_result jsonb;
    v_code text;
BEGIN
    IF operation_id IS NULL OR actor_id IS NULL OR connection_id IS NULL
       OR NOT aap_audit_v1.valid_request_context(audit_context) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, true);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF NOT aap_assets_v1._storage_connection_operation_lock(operation_id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END IF;
    SELECT receipt.* INTO v_saved
    FROM public.storage_connection_operations AS receipt
    WHERE receipt.operation_id = operation_id;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.action, v_saved.connection_id, v_saved.intent)
           IS DISTINCT FROM (actor_id, 'delete'::text, connection_id, '{}'::jsonb) THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
        END IF;
        v_code := aap_assets_v1._storage_connection_access(
            actor_id, v_role, v_saved.connection_scope, v_saved.connection_created_by, 'admin'
        );
        IF v_code IS NOT NULL THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._storage_connection_reply(v_saved.result, NULL);
    END IF;
    BEGIN
        SELECT connection.* INTO v_row
        FROM public.storage_connections AS connection
        WHERE connection.id = connection_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_missing');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id, v_role, v_row.scope, v_row.created_by, 'admin'
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    DELETE FROM public.storage_connections AS connection
    WHERE connection.id = connection_id;
    v_result := pg_catalog.jsonb_build_object(
        'operation_id', operation_id, 'actor_id', actor_id,
        'action', 'delete', 'connection_id', connection_id, 'connection', NULL
    );
    PERFORM aap_audit_v1.record_storage_connection_change(
        operation_id, actor_id, 'delete', connection_id, audit_context,
        pg_catalog.jsonb_build_object('scope', CASE WHEN v_row.scope = 'global' THEN 'global' ELSE 'owner' END)
    );
    INSERT INTO public.storage_connection_operations (
        operation_id, actor_id, action, connection_id, intent,
        connection_scope, connection_created_by, result
    ) VALUES (
        operation_id, actor_id, 'delete', connection_id, '{}'::jsonb,
        CASE WHEN v_row.scope = 'global' THEN 'global' ELSE 'owner' END,
        v_row.created_by, v_result
    );
    RETURN aap_assets_v1._storage_connection_reply(v_result, NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.delete_storage_connection(uuid, uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.record_storage_connection_test(
    operation_id uuid,
    actor_id uuid,
    connection_id uuid,
    source_snapshot text,
    policy_digest text,
    fallback_allowlist jsonb,
    result jsonb,
    audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_actor jsonb;
    v_role text;
    v_saved public.storage_connection_operations%ROWTYPE;
    v_row public.storage_connections%ROWTYPE;
    v_policy jsonb;
    v_code text;
    v_receipt jsonb;
BEGIN
    IF operation_id IS NULL OR actor_id IS NULL OR connection_id IS NULL
       OR source_snapshot IS NULL OR source_snapshot !~ '^[0-9a-f]{64}$'
       OR policy_digest IS NULL OR policy_digest !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(result) IS DISTINCT FROM 'object'
       OR (result - ARRAY['ok', 'message', 'sample_count']) <> '{}'::jsonb
       OR NOT (result ?& ARRAY['ok', 'message', 'sample_count'])
       OR pg_catalog.jsonb_typeof(result -> 'ok') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(result -> 'message') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(result ->> 'message') NOT BETWEEN 1 AND 2048
       OR NOT aap_audit_v1.valid_request_context(audit_context)
       OR (result -> 'ok' = 'true'::jsonb
           AND (result ->> 'message' IS DISTINCT FROM '连接成功'
                OR pg_catalog.jsonb_typeof(result -> 'sample_count') IS DISTINCT FROM 'number'
                OR (result ->> 'sample_count') !~ '^(0|[1-9][0-9]*)$'
                OR (result ->> 'sample_count')::integer NOT BETWEEN 0 AND 20))
       OR (result -> 'ok' = 'false'::jsonb
           AND (result -> 'sample_count' IS DISTINCT FROM 'null'::jsonb
                OR result ->> 'message' !~ '^连接失败: [A-Za-z_][A-Za-z0-9_]{0,127}$'))
       OR result -> 'ok' NOT IN ('true'::jsonb, 'false'::jsonb) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._storage_connection_actor(actor_id, true);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF NOT aap_assets_v1._storage_connection_operation_lock(operation_id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END IF;
    SELECT receipt.* INTO v_saved
    FROM public.storage_connection_operations AS receipt
    WHERE receipt.operation_id = operation_id;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.action, v_saved.connection_id, v_saved.intent)
           IS DISTINCT FROM (actor_id, 'test'::text, connection_id, '{}'::jsonb) THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
        END IF;
        v_code := aap_assets_v1._storage_connection_access(
            actor_id, v_role, v_saved.connection_scope, v_saved.connection_created_by, 'use'
        );
        IF v_code IS NOT NULL THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._storage_connection_reply(v_saved.result, NULL);
    END IF;
    BEGIN
        SELECT connection.* INTO v_row
        FROM public.storage_connections AS connection
        WHERE connection.id = connection_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_missing');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id, v_role, v_row.scope, v_row.created_by, 'use'
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    IF aap_assets_v1._storage_connection_snapshot(v_row) IS DISTINCT FROM source_snapshot THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'source_changed');
    END IF;
    v_policy := aap_assets_v1._storage_connection_policy(fallback_allowlist, true);
    IF v_policy -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_policy;
    END IF;
    IF v_policy #>> '{data,policy_digest}' IS DISTINCT FROM policy_digest THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'policy_changed');
    END IF;
    v_receipt := result || pg_catalog.jsonb_build_object(
        'operation_id', operation_id, 'actor_id', actor_id,
        'connection_id', connection_id, 'action', 'test'
    );
    PERFORM aap_audit_v1.record_storage_connection_change(
        operation_id, actor_id, 'test', connection_id, audit_context,
        pg_catalog.jsonb_build_object('ok', result -> 'ok')
    );
    INSERT INTO public.storage_connection_operations (
        operation_id, actor_id, action, connection_id, intent,
        connection_scope, connection_created_by, result
    ) VALUES (
        operation_id, actor_id, 'test', connection_id, '{}'::jsonb,
        CASE WHEN v_row.scope = 'global' THEN 'global' ELSE 'owner' END,
        v_row.created_by, v_receipt
    );
    RETURN aap_assets_v1._storage_connection_reply(v_receipt, NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'operation_conflict');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_storage_connection_test(uuid, uuid, uuid, text, text, jsonb, jsonb, jsonb) FROM PUBLIC;
