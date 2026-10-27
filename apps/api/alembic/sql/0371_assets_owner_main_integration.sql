-- Forward integration of published main storage identity into Assets owner paths.
-- Wrapped by a feature migration after bo0370; owner SQL artifacts stay frozen.


-- Owner updates must advance the published revision exactly like the retired
-- ORM service did; explicit application increments are left untouched.
CREATE OR REPLACE FUNCTION aap_assets_v1.storage_connection_revision_bump()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.revision IS NOT DISTINCT FROM OLD.revision THEN
        NEW.revision := OLD.revision + 1;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.storage_connection_revision_bump() FROM PUBLIC;
-- aap-statement-break

DROP TRIGGER IF EXISTS storage_connection_revision_bump ON public.storage_connections;
-- aap-statement-break
CREATE TRIGGER storage_connection_revision_bump
BEFORE UPDATE ON public.storage_connections
FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.storage_connection_revision_bump();
-- aap-statement-break

-- Owner updates must also keep the registered connector location's configuration
-- revision aligned with main's retired service: any accepted configuration or
-- credential change advances it exactly once.
CREATE OR REPLACE FUNCTION aap_assets_v1._connector_location_id(p_connection_id uuid)
RETURNS uuid
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT (
        pg_catalog.substr(p_md5, 1, 8) || '-' || pg_catalog.substr(p_md5, 9, 4)
        || '-' || pg_catalog.substr(p_md5, 13, 4) || '-'
        || pg_catalog.substr(p_md5, 17, 4) || '-'
        || pg_catalog.substr(p_md5, 21, 12)
    )::uuid
    FROM (SELECT pg_catalog.md5(
        'aap:storage-connection:' || p_connection_id::text
    ) AS p_md5) AS digest
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._connector_location_id(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._storage_connection_location_referenced(
    p_location_id uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_relation record;
    v_found boolean;
BEGIN
    IF p_location_id IS NULL THEN
        RETURN false;
    END IF;
    FOR v_relation IN
        SELECT * FROM (VALUES
            ('dataset_asset_locations', 'location_id', false),
            ('managed_original_creations', 'location_id', false),
            ('dataset_sources', 'location_id', false),
            ('original_cache_entries', 'location_id', false),
            ('managed_original_retirements', 'location_id', false),
            ('dataset_auxiliary_originals', 'location_id', false),
            ('task_originals', 'location_id', false),
            ('storage_bindings', 'location_id', false),
            ('thumbnail_outputs', 'storage_ref', true),
            ('image_pyramid_outputs', 'storage_ref', true),
            ('video_derivative_outputs', 'storage_ref', true),
            ('prediction_input_leases', 'storage_ref', true),
            ('mask_format_uploads', 'storage_ref', true),
            ('mask_format_imports', 'staging_ref', true),
            ('comment_attachment_uploads', 'storage_ref', true),
            ('avatar_uploads', 'storage_ref', true),
            ('guide_asset_uploads', 'storage_ref', true),
            ('raster_mask_objects', 'storage_ref', true),
            ('bug_attachment_uploads', 'storage_ref', true),
            ('audit_archive_outputs', 'storage_ref', true),
            ('export_outputs', 'storage_ref', true),
            ('export_media_bundles', 'storage_ref', true),
            ('training_dataset_versions', 'storage_ref', true),
            ('training_runs', 'storage_ref', true),
            ('training_previews', 'storage_ref', true),
            ('sample_evaluation_runs', 'storage_ref', true),
            ('active_learning_rounds', 'storage_ref', true),
            ('training_storage_deletions', 'storage_ref', true)
        ) AS relations(table_name, column_name, json_reference)
    LOOP
        IF pg_catalog.to_regclass(
            pg_catalog.format('public.%I', v_relation.table_name)
        ) IS NULL THEN
            CONTINUE;
        END IF;
        IF v_relation.json_reference THEN
            EXECUTE pg_catalog.format(
                'SELECT EXISTS (SELECT 1 FROM public.%I WHERE %I->>''location_id'' = $1 LIMIT 1)',
                v_relation.table_name, v_relation.column_name
            ) INTO v_found USING p_location_id::text;
        ELSE
            EXECUTE pg_catalog.format(
                'SELECT EXISTS (SELECT 1 FROM public.%I WHERE %I = $1 LIMIT 1)',
                v_relation.table_name, v_relation.column_name
            ) INTO v_found USING p_location_id;
        END IF;
        IF v_found THEN
            RETURN true;
        END IF;
    END LOOP;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_location_referenced(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.create_storage_connection(
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
    v_location_id uuid;
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
    v_location_id := aap_assets_v1._connector_location_id(v_row.id);
    INSERT INTO public.storage_locations (
        id, name, kind, config_revision, config, connection_id, enabled
    ) VALUES (
        v_location_id, 'connector-' || v_row.id::text, v_row.kind, 1,
        '{}'::jsonb, v_row.id, true
    );
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

CREATE OR REPLACE FUNCTION aap_assets_v1.update_storage_connection(
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
    v_location public.storage_locations%ROWTYPE;
    v_relocation boolean;
    v_before public.storage_connections%ROWTYPE;
    v_config_changed boolean;
    v_secret_changed boolean;
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

    SELECT location.* INTO v_location
    FROM public.storage_locations AS location
    WHERE location.connection_id = connection_id
    FOR UPDATE NOWAIT;
    v_relocation := FOUND;
    IF v_relocation AND (intent -> 'config') IS DISTINCT FROM 'null'::jsonb
       AND aap_assets_v1._storage_connection_location_referenced(v_location.id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    END IF;

    v_name := COALESCE(intent ->> 'name', v_row.name);
    v_config := config;
    v_secret := CASE WHEN secret_enc IS NULL THEN v_row.secret_enc ELSE pg_catalog.decode(secret_enc, 'base64') END;
    -- Main CAS contract: every accepted non-replay edit advances the public
    -- connection revision, including a name-only or identical-configuration
    -- save. Only an actual credential/configuration change advances the
    -- registered location revision.
    v_before := v_row;
    v_config_changed := v_config IS DISTINCT FROM v_before.config;
    v_secret_changed := v_secret IS DISTINCT FROM v_before.secret_enc;
    UPDATE public.storage_connections AS connection
    SET name = v_name,
        config = v_config,
        secret_enc = v_secret,
        revision = v_before.revision + 1,
        updated_at = pg_catalog.clock_timestamp()
    WHERE connection.id = connection_id
    RETURNING connection.* INTO v_row;
    IF v_relocation AND (v_config_changed OR v_secret_changed) THEN
        UPDATE public.storage_locations AS location
        SET config_revision = location.config_revision + 1,
            kind = v_row.kind,
            config = '{}'::jsonb
        WHERE location.id = v_location.id;
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

CREATE OR REPLACE FUNCTION aap_assets_v1.delete_storage_connection(
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
    v_location public.storage_locations%ROWTYPE;
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

    SELECT connection.* INTO v_row
    FROM public.storage_connections AS connection
    WHERE connection.id = connection_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_missing');
    END IF;
    v_code := aap_assets_v1._storage_connection_access(
        actor_id, v_role, v_row.scope, v_row.created_by, 'admin'
    );
    IF v_code IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, v_code);
    END IF;
    SELECT location.* INTO v_location
    FROM public.storage_locations AS location
    WHERE location.connection_id = connection_id
    FOR UPDATE NOWAIT;
    IF FOUND THEN
        IF aap_assets_v1._storage_connection_location_referenced(v_location.id) THEN
            RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
        END IF;
        DELETE FROM public.storage_locations AS location
        WHERE location.id = v_location.id;
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
    WHEN foreign_key_violation THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'connection_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.delete_storage_connection(uuid, uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

-- Facts must expose the registered location and its configuration revision so
-- callers can CAS both identities before any managed write.
CREATE OR REPLACE FUNCTION aap_assets_v1._storage_connection_facts(
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
        'revision', (p_connection).revision,
        'name', (p_connection).name,
        'kind', (p_connection).kind,
        'config', (p_connection).config,
        'scope', (p_connection).scope,
        'project_id', (p_connection).project_id,
        'secret_set', (p_connection).secret_enc IS NOT NULL,
        'created_by', (p_connection).created_by,
        'created_at', (p_connection).created_at,
        'updated_at', (p_connection).updated_at,
        'location_id', (
            SELECT location.id FROM public.storage_locations AS location
            WHERE location.connection_id = (p_connection).id
        ),
        'location_config_revision', (
            SELECT location.config_revision FROM public.storage_locations AS location
            WHERE location.connection_id = (p_connection).id
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._storage_connection_facts(public.storage_connections) FROM PUBLIC;
-- aap-statement-break

-- Managed original creation for every Assets source producer. Allocation
-- reserves the immutable locator before any physical I/O; evidence recording
-- advances the durable lifecycle only with version-pinned readback proof; and
-- business item publication is refused unless the matching creation is verified.
CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_endpoint(p_value text)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_value text := pg_catalog.btrim(p_value);
BEGIN
    IF v_value IS NULL OR pg_catalog.char_length(v_value) NOT BETWEEN 1 AND 2048
       OR v_value <> pg_catalog.btrim(v_value)
       OR pg_catalog.right(v_value, 1) = '/'
       OR pg_catalog.strpos(v_value, pg_catalog.chr(92)) > 0
       OR v_value !~ '^https?://[^/?#@[:space:]]+$'
       OR v_value ~ '[^[:print:]]' THEN
        RETURN NULL;
    END IF;
    RETURN v_value;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_endpoint(text) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_prefix(p_base text)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_base text := COALESCE(p_base, '');
    v_parts text[];
    v_result text;
BEGIN
    IF v_base <> pg_catalog.btrim(v_base)
       OR pg_catalog.strpos(v_base, pg_catalog.chr(92)) > 0
       OR v_base ~ '[[:cntrl:]]'
       OR pg_catalog.right(v_base, 1) = '/' THEN
        RETURN NULL;
    END IF;
    v_base := pg_catalog.rtrim(v_base, '/');
    IF v_base <> '' THEN
        v_parts := pg_catalog.string_to_array(v_base, '/');
        IF EXISTS (
            SELECT 1 FROM pg_catalog.unnest(v_parts) AS part(value)
            WHERE part.value = '' OR part.value IN ('.', '..')
        ) THEN
            RETURN NULL;
        END IF;
    END IF;
    v_result := CASE WHEN v_base = '' THEN '' ELSE v_base || '/' END
        || '_aap_managed/datasets/';
    IF pg_catalog.char_length(v_result) > 891
       OR pg_catalog.octet_length(v_result) > 915 THEN
        RETURN NULL;
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_prefix(text) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_identity(
    p_endpoint text,
    p_bucket text
)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_array(
                    pg_catalog.rtrim(p_endpoint, '/'), p_bucket
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_identity(text, text) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_gate_revision(p_location_id uuid)
RETURNS integer
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_state text;
    v_revision integer;
BEGIN
    INSERT INTO public.storage_maintenance_gates (location_id)
    VALUES (p_location_id)
    ON CONFLICT (location_id) DO NOTHING;
    SELECT gate.state, gate.revision INTO v_state, v_revision
    FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id = p_location_id
    FOR SHARE;
    IF NOT FOUND OR v_state IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'managed_original_maintenance_unavailable';
    END IF;
    RETURN v_revision;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_gate_revision(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_request_valid(
    p_request jsonb
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_request jsonb := p_request;
BEGIN
    RETURN COALESCE(
        v_request IS NOT NULL
        AND pg_catalog.jsonb_typeof(v_request) = 'object'
        AND (v_request - ARRAY[
            'dataset_id', 'item_id', 'publication_id', 'origin', 'mode',
            'file_path', 'logical_path', 'file_type', 'content_type',
            'max_bytes', 'authority', 'staging', 'requested_by'
        ]) = '{}'::jsonb
        AND (v_request ?& ARRAY[
            'dataset_id', 'item_id', 'publication_id', 'origin', 'mode',
            'file_path', 'logical_path', 'file_type', 'content_type',
            'max_bytes', 'authority', 'staging'
        ])
        AND pg_catalog.jsonb_typeof(v_request -> 'dataset_id') = 'string'
        AND (v_request ->> 'dataset_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND pg_catalog.jsonb_typeof(v_request -> 'item_id') = 'string'
        AND (v_request ->> 'item_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND pg_catalog.jsonb_typeof(v_request -> 'publication_id') = 'string'
        AND (v_request ->> 'publication_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND v_request ->> 'origin' IN ('direct', 'zip', 'connector', 'directory_copy')
        AND v_request ->> 'mode' IN ('write', 'adopt')
        AND (v_request ->> 'mode' = 'write'
             OR v_request ->> 'origin' = 'directory_copy')
        AND pg_catalog.jsonb_typeof(v_request -> 'file_path') = 'string'
        AND pg_catalog.char_length(v_request ->> 'file_path') BETWEEN 1 AND 1000
        AND pg_catalog.octet_length(v_request ->> 'file_path') <= 1024
        AND (v_request ->> 'file_path') !~ '[[:cntrl:]]'
        AND pg_catalog.left(v_request ->> 'file_path', 1) <> '/'
        AND pg_catalog.strpos(v_request ->> 'file_path', pg_catalog.chr(92)) = 0
        AND pg_catalog.jsonb_typeof(v_request -> 'logical_path') = 'string'
        AND aap_assets_v1._dataset_import_relative_path_valid(
            v_request ->> 'logical_path'
        )
        AND v_request ->> 'file_type' IN ('image', 'video', 'point_cloud', 'other')
        AND pg_catalog.jsonb_typeof(v_request -> 'content_type') = 'string'
        AND pg_catalog.char_length(v_request ->> 'content_type') BETWEEN 1 AND 255
        AND (v_request ->> 'content_type') = pg_catalog.btrim(v_request ->> 'content_type')
        AND (v_request ->> 'content_type') !~ '[[:cntrl:]]'
        AND pg_catalog.jsonb_typeof(v_request -> 'max_bytes') = 'number'
        AND (v_request ->> 'max_bytes') ~ '^(0|[1-9][0-9]*)$'
        AND (v_request ->> 'max_bytes')::numeric <= 9223372036854775807
        AND pg_catalog.jsonb_typeof(v_request -> 'authority') = 'object'
        AND (v_request -> 'authority' ->> 'kind') IN (
            'dataset_import', 'dataset_upload', 'dataset_source_ingest', 'operator_item'
        )
        AND (
            (v_request -> 'staging') = 'null'::jsonb
            OR (
                pg_catalog.jsonb_typeof(v_request -> 'staging') = 'object'
                AND (v_request -> 'staging' - ARRAY['bucket', 'key']) = '{}'::jsonb
                AND (v_request -> 'staging' ?& ARRAY['bucket', 'key'])
                AND pg_catalog.jsonb_typeof(v_request -> 'staging' -> 'bucket') = 'string'
                AND pg_catalog.char_length(v_request -> 'staging' ->> 'bucket') BETWEEN 1 AND 255
                AND pg_catalog.jsonb_typeof(v_request -> 'staging' -> 'key') = 'string'
                AND pg_catalog.char_length(v_request -> 'staging' ->> 'key') BETWEEN 1 AND 1000
                AND pg_catalog.octet_length(v_request -> 'staging' ->> 'key') <= 1024
            )
        )
        AND (
            NOT (v_request ? 'requested_by')
            OR (v_request -> 'requested_by') = 'null'::jsonb
            OR (
                pg_catalog.jsonb_typeof(v_request -> 'requested_by') = 'string'
                AND (v_request ->> 'requested_by') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
            )
        ),
        false
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_request_valid(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.begin_managed_original(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_item_id uuid;
    v_publication_id uuid;
    v_origin text;
    v_mode text;
    v_file_path text;
    v_logical_path text;
    v_file_type text;
    v_content_type text;
    v_max_bytes bigint;
    v_authority jsonb;
    v_kind text;
    v_staging jsonb;
    v_requested_by uuid;
    v_existing public.managed_original_creations%ROWTYPE;
    v_binding public.storage_bindings%ROWTYPE;
    v_import_binding public.storage_bindings%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_endpoint text;
    v_prefix text;
    v_bucket text;
    v_key text;
    v_identity text;
    v_ref jsonb;
    v_config jsonb;
    v_fingerprint text;
    v_revision integer;
    v_timeout integer;
    v_staging_key text;
    v_creation public.managed_original_creations%ROWTYPE;
    v_effective_publication uuid;
    v_effective_path text;
    v_destination jsonb;
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_operation public.dataset_import_operations%ROWTYPE;
    v_entry public.dataset_import_entries%ROWTYPE;
    v_ingest public.dataset_source_ingest_operations%ROWTYPE;
    v_ingest_entry public.dataset_source_ingest_entries%ROWTYPE;
BEGIN
    IF NOT aap_assets_v1._managed_original_request_valid(p_request) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    v_dataset_id := (p_request ->> 'dataset_id')::uuid;
    v_item_id := (p_request ->> 'item_id')::uuid;
    v_publication_id := (p_request ->> 'publication_id')::uuid;
    v_origin := p_request ->> 'origin';
    v_mode := p_request ->> 'mode';
    v_file_path := p_request ->> 'file_path';
    v_logical_path := p_request ->> 'logical_path';
    v_file_type := p_request ->> 'file_type';
    v_content_type := p_request ->> 'content_type';
    v_max_bytes := (p_request ->> 'max_bytes')::bigint;
    v_authority := p_request -> 'authority';
    v_kind := v_authority ->> 'kind';
    v_staging := p_request -> 'staging';
    v_requested_by := CASE
        WHEN p_request ? 'requested_by'
         AND (p_request -> 'requested_by') <> 'null'::jsonb
        THEN (p_request ->> 'requested_by')::uuid
        ELSE NULL
    END;
    v_effective_publication := v_publication_id;
    v_effective_path := v_file_path;

    IF v_kind = 'dataset_import' THEN
        IF v_origin <> 'connector' OR v_mode <> 'write'
           OR NOT (v_authority ?& ARRAY['job_id', 'entry_index', 'publication_id', 'item_id'])
           OR (v_authority - ARRAY['kind', 'job_id', 'entry_index', 'publication_id', 'item_id']) <> '{}'::jsonb
           OR (v_authority ->> 'item_id') IS DISTINCT FROM v_item_id::text
           OR (v_authority ->> 'publication_id') IS DISTINCT FROM v_publication_id::text
           OR (v_authority ->> 'item_id') IS DISTINCT FROM v_item_id::text THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        SELECT operation.* INTO v_operation
        FROM public.dataset_import_operations AS operation
        WHERE operation.job_id = (v_authority ->> 'job_id')::uuid;
        IF NOT FOUND OR v_operation.dataset_id IS DISTINCT FROM v_dataset_id THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        SELECT entry.* INTO v_entry
        FROM public.dataset_import_entries AS entry
        WHERE entry.job_id = v_operation.job_id
          AND entry.entry_index = (v_authority ->> 'entry_index')::bigint;
        IF NOT FOUND
           OR v_entry.status <> 'pending'
           OR v_entry.item_id IS DISTINCT FROM v_item_id
           OR v_entry.publication_id IS DISTINCT FROM v_publication_id
           OR v_entry.file_path IS DISTINCT FROM v_file_path THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF NOT aap_assets_v1._managed_original_source_gate_active(
            v_operation.connection_id
        ) THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        IF v_staging <> 'null'::jsonb THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_kind = 'dataset_upload' THEN
        IF v_origin <> 'direct' OR v_mode <> 'write'
           OR NOT (v_authority ?& ARRAY['item_id', 'publication_id'])
           OR (v_authority - ARRAY['kind', 'item_id', 'publication_id']) <> '{}'::jsonb THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF v_publication_id IS NULL THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = v_item_id
        FOR UPDATE;
        IF NOT FOUND
           OR v_intent.dataset_id IS DISTINCT FROM v_dataset_id
           OR v_intent.actor_id IS DISTINCT FROM v_requested_by
           OR v_intent.state <> 'pending'
           OR v_staging = 'null'::jsonb
           OR (v_staging ->> 'key') IS DISTINCT FROM v_intent.staging_key THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF v_intent.publication_id IS NULL THEN
            v_effective_publication := pg_catalog.gen_random_uuid();
            v_effective_path := 'dataset-sources/' || v_dataset_id::text || '/'
                || v_item_id::text || '/' || v_effective_publication::text || '/'
                || v_intent.file_name;
            IF pg_catalog.char_length(v_effective_path) > 1000
               OR pg_catalog.octet_length(v_effective_path) > 1024 THEN
                RETURN aap_assets_v1._managed_original_reply(
                    NULL, 'resource_budget_exceeded'
                );
            END IF;
            UPDATE public.dataset_upload_intents AS intent
            SET publication_id = v_effective_publication,
                file_path = v_effective_path,
                updated_at = pg_catalog.clock_timestamp()
            WHERE intent.item_id = v_item_id
              AND intent.state = 'pending';
            IF NOT FOUND THEN
                RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
            END IF;
            v_authority := pg_catalog.jsonb_set(
                v_authority, '{publication_id}',
                pg_catalog.to_jsonb(v_effective_publication::text)
            );
        ELSIF v_intent.publication_id IS DISTINCT FROM v_publication_id
           OR v_intent.file_path IS DISTINCT FROM v_file_path THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
    ELSIF v_kind = 'dataset_source_ingest' THEN
        IF v_origin NOT IN ('zip', 'directory_copy')
           OR v_mode NOT IN ('write', 'adopt')
           OR (v_mode = 'adopt' AND v_origin <> 'directory_copy')
           OR NOT (v_authority ?& ARRAY['operation_id', 'batch_id', 'entry_index', 'item_id'])
           OR (v_authority - ARRAY[
                'kind', 'operation_id', 'batch_id', 'entry_index', 'publication_id', 'item_id'
           ]) <> '{}'::jsonb
           OR (v_authority ->> 'item_id') IS DISTINCT FROM v_item_id::text THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        SELECT operation.* INTO v_ingest
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = (v_authority ->> 'operation_id')::uuid;
        IF NOT FOUND OR v_ingest.dataset_id IS DISTINCT FROM v_dataset_id THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF (v_ingest.kind = 'zip' AND v_mode <> 'write')
           OR (v_ingest.kind = 'scan' AND v_mode NOT IN ('adopt', 'write'))
           OR (v_ingest.kind = 'zip' AND v_origin <> 'zip')
           OR (v_ingest.kind = 'scan' AND v_origin <> 'directory_copy') THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        SELECT entry.* INTO v_ingest_entry
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = v_ingest.operation_id
          AND entry.batch_id = (v_authority ->> 'batch_id')::uuid
          AND entry.entry_index = (v_authority ->> 'entry_index')::integer;
        IF NOT FOUND
           OR v_ingest_entry.planned_status <> 'pending'
           OR v_ingest_entry.item_id IS DISTINCT FROM v_item_id
           OR v_ingest_entry.publication_id IS DISTINCT FROM
              NULLIF(v_authority ->> 'publication_id', '')::uuid
           OR v_ingest_entry.file_path IS DISTINCT FROM v_file_path THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF v_staging <> 'null'::jsonb THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_kind = 'operator_item' THEN
        IF v_origin <> 'directory_copy' OR v_mode <> 'write'
           OR NOT (v_authority ?& ARRAY['operation_id', 'item_id', 'publication_id'])
           OR (v_authority - ARRAY['kind', 'operation_id', 'item_id', 'publication_id']) <> '{}'::jsonb
           OR (v_authority ->> 'item_id') IS DISTINCT FROM v_item_id::text
           OR (v_authority ->> 'publication_id') IS DISTINCT FROM v_publication_id::text THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.datasets AS dataset WHERE dataset.id = v_dataset_id
        ) OR EXISTS (
            SELECT 1 FROM public.dataset_items AS item WHERE item.id = v_item_id
        ) THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        IF v_staging <> 'null'::jsonb THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
    ELSE
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;

    SELECT creation.* INTO v_existing
    FROM public.managed_original_creations AS creation
    WHERE creation.owner_kind = 'dataset'
      AND creation.dataset_id = v_dataset_id
      AND creation.authority = v_authority
    ORDER BY creation.allocated_at DESC, creation.id DESC
    LIMIT 1
    FOR UPDATE;
    IF FOUND THEN
        IF v_existing.max_bytes IS DISTINCT FROM v_max_bytes
           OR v_existing.content_type IS DISTINCT FROM v_content_type
           OR v_existing.origin IS DISTINCT FROM v_origin
           OR v_existing.logical_path IS DISTINCT FROM v_logical_path
           OR v_existing.configuration ->> 'mode' IS DISTINCT FROM v_mode THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
        END IF;
        IF v_origin = 'direct'
           AND (v_staging = 'null'::jsonb
                OR v_existing.staging_key IS DISTINCT FROM (v_staging ->> 'key')) THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
        END IF;
        IF v_existing.status = 'verified' THEN
            RETURN aap_assets_v1._managed_original_reply(
                aap_assets_v1._managed_original_facts(v_existing), NULL
            );
        ELSIF v_existing.status IN ('failed', 'conflict', 'unknown') THEN
            IF v_kind = 'dataset_upload'
               AND v_existing.authority = v_authority THEN
                v_effective_publication := pg_catalog.gen_random_uuid();
                v_effective_path := 'dataset-sources/' || v_dataset_id::text || '/'
                    || v_item_id::text || '/' || v_effective_publication::text || '/'
                    || v_intent.file_name;
                IF pg_catalog.char_length(v_effective_path) > 1000
                   OR pg_catalog.octet_length(v_effective_path) > 1024 THEN
                    RETURN aap_assets_v1._managed_original_reply(NULL, 'resource_budget_exceeded');
                END IF;
                UPDATE public.dataset_upload_intents AS intent
                SET publication_id = v_effective_publication,
                    file_path = v_effective_path,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE intent.item_id = v_item_id
                  AND intent.state = 'pending';
                IF NOT FOUND THEN
                    RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
                END IF;
                v_authority := pg_catalog.jsonb_set(
                    v_authority, '{publication_id}',
                    pg_catalog.to_jsonb(v_effective_publication::text)
                );
            ELSE
                RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
            END IF;
        ELSE
            IF NOT EXISTS (
                SELECT 1
                FROM public.storage_bindings AS binding
                JOIN public.storage_locations AS location
                  ON location.id = binding.location_id
                WHERE binding.purpose = 'datasets'
                  AND binding.location_id = v_existing.location_id
                  AND binding.bucket = v_existing.bucket
                  AND binding.generation
                      = (v_existing.configuration -> 'original_ref' ->> 'binding_generation')::integer
                  AND location.config_revision
                      = (v_existing.configuration -> 'original_ref' ->> 'config_revision')::integer
            ) THEN
                RETURN aap_assets_v1._managed_original_reply(
                    NULL, 'creation_configuration_changed'
                );
            END IF;
            RETURN aap_assets_v1._managed_original_reply(
                aap_assets_v1._managed_original_facts(v_existing), NULL
            );
        END IF;
    END IF;

    SELECT binding.* INTO v_binding
    FROM public.storage_bindings AS binding
    WHERE binding.purpose = 'datasets';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    SELECT location.* INTO v_location
    FROM public.storage_locations AS location
    WHERE location.id = v_binding.location_id
    FOR SHARE;
    IF NOT FOUND OR NOT v_location.enabled OR v_location.kind <> 's3'
       OR v_location.config_revision < 1 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    v_bucket := v_binding.bucket;
    IF v_location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid THEN
        IF v_location.connection_id IS NOT NULL
           OR v_location.config_revision <> 1
           OR v_binding.generation <> 1
           OR pg_catalog.jsonb_typeof(v_location.config -> 'use_ssl') <> 'boolean'
           OR pg_catalog.jsonb_typeof(v_location.config -> 'endpoint') <> 'string' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_endpoint := aap_assets_v1._managed_original_endpoint(
            CASE WHEN (v_location.config ->> 'use_ssl')::boolean
                THEN 'https://' ELSE 'http://' END
            || pg_catalog.btrim(v_location.config ->> 'endpoint')
        );
        v_prefix := '';
    ELSE
        SELECT connection.* INTO v_connection
        FROM public.storage_connections AS connection
        WHERE connection.id = v_location.connection_id
        FOR SHARE;
        IF NOT FOUND OR v_connection.kind <> 's3'
           OR v_connection.scope <> 'global'
           OR v_connection.project_id IS NOT NULL
           OR v_connection.secret_enc IS NULL
           OR v_connection.revision < 1
           OR pg_catalog.jsonb_typeof(v_connection.config -> 'bucket') <> 'string' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_bucket := v_connection.config ->> 'bucket';
        IF v_bucket IS DISTINCT FROM v_binding.bucket THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_endpoint := aap_assets_v1._managed_original_endpoint(
            v_connection.config ->> 'endpoint'
        );
        v_prefix := aap_assets_v1._managed_original_prefix(
            v_connection.config ->> 'base_prefix'
        );
        IF v_prefix IS NULL
           OR (v_binding.generation = 1
               AND v_location.id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid) THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
    END IF;
    -- A legacy platform binding may pin the existing scanned object (adopt);
    -- a connection-backed binding must receive a conditional managed copy.
    IF v_kind = 'dataset_source_ingest'
       AND v_ingest.kind = 'scan'
       AND v_location.id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid THEN
        v_mode := 'write';
    END IF;
    IF v_endpoint IS NULL
       OR pg_catalog.char_length(v_bucket) NOT BETWEEN 1 AND 255
       OR v_bucket ~ '[[:space:][:cntrl:]/?#@%:]'
       OR pg_catalog.strpos(v_bucket, pg_catalog.chr(92)) > 0 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    v_key := v_prefix || v_effective_path;
    IF pg_catalog.char_length(v_key) > 1000 OR pg_catalog.octet_length(v_key) > 1024 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_budget_exceeded');
    END IF;
    v_identity := aap_assets_v1._managed_original_identity(v_endpoint, v_bucket);
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 1,
        'purpose', 'datasets',
        'location_id', v_location.id::text,
        'config_revision', v_location.config_revision,
        'binding_generation', v_binding.generation,
        'endpoint_url', v_endpoint,
        'bucket', v_bucket,
        'key_prefix', v_prefix,
        'object_key', v_key,
        'storage_identity', v_identity
    );
    v_config := pg_catalog.jsonb_build_object(
        'schema', 2,
        'endpoint', v_endpoint,
        'buckets', pg_catalog.jsonb_build_object('datasets', v_bucket),
        'bindings', pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'purpose', 'datasets',
            'location_id', v_location.id,
            'bucket', v_bucket,
            'generation', v_binding.generation
        )),
        'location', pg_catalog.to_jsonb(v_location),
        'connection_revision', CASE
            WHEN v_location.connection_id IS NULL THEN NULL
            ELSE v_connection.revision
        END,
        'mode', v_mode,
        'original_ref', v_ref
    );
    v_fingerprint := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_config::text, 'UTF8'), 'sha256'), 'hex'
    );
    BEGIN
        v_revision := aap_assets_v1._managed_original_gate_revision(v_location.id);
    EXCEPTION WHEN SQLSTATE '55006' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END;
    v_timeout := GREATEST(
        60,
        LEAST(
            3600,
            pg_catalog.ceil(v_max_bytes::numeric / 8388608)::integer
        )
    );

    IF v_origin = 'direct' THEN
        SELECT binding.* INTO v_import_binding
        FROM public.storage_bindings AS binding
        WHERE binding.purpose = 'import';
        IF NOT FOUND
           OR pg_catalog.jsonb_typeof(v_staging -> 'key') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_staging ->> 'key') NOT BETWEEN 1 AND 1000 THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_staging_key := v_staging ->> 'key';
    ELSE
        v_staging_key := NULL;
    END IF;

    BEGIN
        INSERT INTO public.managed_original_creations (
            id, owner_kind, dataset_id, project_id, task_id, location_id,
            storage_identity, bucket, object_key, logical_path, origin,
            max_bytes, timeout_seconds, content_type, requested_by, authority,
            configuration, input_fingerprint, maintenance_revision,
            staging_bucket, staging_key, staging_ref, status
        ) VALUES (
            pg_catalog.gen_random_uuid(), 'dataset', v_dataset_id, NULL, NULL,
            v_location.id, v_identity, v_bucket, v_key, v_logical_path, v_origin,
            v_max_bytes, v_timeout, v_content_type, v_requested_by, v_authority,
            v_config, v_fingerprint, v_revision,
            CASE WHEN v_origin = 'direct' THEN v_import_binding.bucket ELSE NULL END,
            v_staging_key, NULL, 'allocated'
        ) RETURNING * INTO v_creation;
    EXCEPTION WHEN unique_violation THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_key_conflict');
    END;

    IF v_location.connection_id IS NOT NULL THEN
        v_destination := pg_catalog.jsonb_build_object(
            'kind', v_connection.kind,
            'config', v_connection.config,
            'secret_enc', pg_catalog.replace(
                pg_catalog.encode(v_connection.secret_enc, 'base64'), E'\n', ''
            )
        );
    ELSE
        v_destination := NULL;
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        aap_assets_v1._managed_original_facts(v_creation), NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_managed_original(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_reply(
    p_data jsonb,
    p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
            'schema', 'aap-managed-original/v1',
            'ok', true,
            'data', p_data
        )
        ELSE pg_catalog.jsonb_build_object(
            'schema', 'aap-managed-original/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_facts(
    p_creation public.managed_original_creations
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ref jsonb := (p_creation).configuration -> 'original_ref';
    v_prefix text := COALESCE((p_creation).configuration -> 'original_ref' ->> 'key_prefix', '');
    v_destination jsonb;
BEGIN
    SELECT pg_catalog.jsonb_build_object(
        'kind', connection.kind,
        'config', connection.config,
        'secret_enc', pg_catalog.replace(
            pg_catalog.encode(connection.secret_enc, 'base64'), E'\n', ''
        )
    ) INTO v_destination
    FROM public.storage_connections AS connection
    WHERE connection.id = ((p_creation).configuration -> 'location' ->> 'connection_id')::uuid;
    RETURN pg_catalog.jsonb_build_object(
        'creation_id', (p_creation).id,
        'dataset_id', (p_creation).dataset_id,
        'location_id', (p_creation).location_id,
        'connection_id', (p_creation).configuration -> 'location' -> 'connection_id',
        'connection_revision', (p_creation).configuration -> 'connection_revision',
        'bucket', (p_creation).bucket,
        'object_key', (p_creation).object_key,
        'key_prefix', v_prefix,
        'endpoint_url', v_ref ->> 'endpoint_url',
        'storage_identity', (p_creation).storage_identity,
        'config_revision', (v_ref ->> 'config_revision')::integer,
        'binding_generation', (v_ref ->> 'binding_generation')::integer,
        'max_bytes', (p_creation).max_bytes,
        'content_type', (p_creation).content_type,
        'origin', (p_creation).origin,
        'mode', COALESCE((p_creation).configuration ->> 'mode', 'write'),
        'staging', CASE
            WHEN (p_creation).staging_bucket IS NULL THEN NULL
            ELSE pg_catalog.jsonb_build_object(
                'bucket', (p_creation).staging_bucket,
                'key', (p_creation).staging_key
            )
        END,
        'destination', v_destination,
        'publication_id', NULLIF((p_creation).authority ->> 'publication_id', '')::uuid,
        'file_path', (p_creation).object_key
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_facts(public.managed_original_creations) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_proof_valid(p_evidence jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_evidence jsonb := p_evidence;
BEGIN
    IF v_evidence IS NULL OR pg_catalog.jsonb_typeof(v_evidence) <> 'object'
       OR (v_evidence - ARRAY[
            'outcome', 'mode', 'physical_outcome', 'sha256', 'md5',
            'size_bytes', 'etag', 'version_id', 'request_id', 'prepared',
            'verification', 'observed', 'error_code'
       ]) <> '{}'::jsonb
       OR NOT (v_evidence ?& ARRAY['outcome', 'mode', 'physical_outcome'])
       OR (v_evidence ->> 'outcome') NOT IN ('verified', 'failed', 'conflict', 'unknown')
       OR (v_evidence ->> 'mode') NOT IN ('write', 'adopt')
       OR (v_evidence ->> 'physical_outcome') NOT IN ('not_written', 'written', 'unknown') THEN
        RETURN false;
    END IF;
    IF (v_evidence ->> 'outcome') = 'verified' THEN
        RETURN COALESCE(
            pg_catalog.jsonb_typeof(v_evidence -> 'sha256') = 'string'
            AND (v_evidence ->> 'sha256') ~ '^[0-9a-f]{64}$'
            AND pg_catalog.jsonb_typeof(v_evidence -> 'md5') = 'string'
            AND (v_evidence ->> 'md5') ~ '^[0-9a-f]{32}$'
            AND pg_catalog.jsonb_typeof(v_evidence -> 'size_bytes') = 'number'
            AND (v_evidence ->> 'size_bytes') ~ '^(0|[1-9][0-9]*)$'
            AND pg_catalog.jsonb_typeof(v_evidence -> 'etag') = 'string'
            AND pg_catalog.char_length(v_evidence ->> 'etag') BETWEEN 1 AND 1024
            AND pg_catalog.jsonb_typeof(v_evidence -> 'verification') = 'object'
            AND (v_evidence -> 'verification' ->> 'sha256') = (v_evidence ->> 'sha256')
            AND (v_evidence -> 'verification' ->> 'md5') = (v_evidence ->> 'md5')
            AND (v_evidence -> 'verification' ->> 'size_bytes')
                = (v_evidence ->> 'size_bytes')
            AND (v_evidence -> 'verification' ->> 'etag') = (v_evidence ->> 'etag')
            AND (v_evidence -> 'verification' ->> 'version_id')
                IS NOT DISTINCT FROM (v_evidence ->> 'version_id')
            AND (v_evidence ->> 'physical_outcome') = 'written'
            AND (
                NOT (v_evidence ? 'version_id')
                OR (v_evidence -> 'version_id') = 'null'::jsonb
                OR (
                    pg_catalog.jsonb_typeof(v_evidence -> 'version_id') = 'string'
                    AND pg_catalog.char_length(v_evidence ->> 'version_id') BETWEEN 1 AND 1024
                    AND (v_evidence ->> 'version_id') <> 'null'
                )
            )
            AND (
                NOT (v_evidence ? 'prepared')
                OR (v_evidence -> 'prepared') = 'null'::jsonb
                OR pg_catalog.jsonb_typeof(v_evidence -> 'prepared') = 'object'
            ),
            false
        );
    END IF;
    RETURN COALESCE(
        pg_catalog.jsonb_typeof(v_evidence -> 'error_code') = 'string'
        AND (v_evidence ->> 'error_code') ~ '^[a-z][a-z0-9_]{0,79}$',
        false
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_proof_valid(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.record_managed_original_proof(
    p_creation_id uuid,
    p_evidence jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.managed_original_creations%ROWTYPE;
    v_outcome text;
    v_sha256 text;
    v_md5 text;
    v_size_bytes bigint;
    v_etag text;
    v_version_id text;
    v_request_id text;
    v_prepared jsonb;
    v_receipt jsonb;
    v_verification jsonb;
    v_observed jsonb;
    v_revision integer;
    v_binding public.storage_bindings%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_operation_id uuid;
BEGIN
    IF p_creation_id IS NULL OR NOT aap_assets_v1._managed_original_proof_valid(p_evidence) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    SELECT creation.* INTO v_row
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    v_outcome := p_evidence ->> 'outcome';
    IF v_row.status = 'verified' THEN
        IF v_outcome = 'verified'
           AND v_row.sha256 = (p_evidence ->> 'sha256')
           AND v_row.size_bytes = (p_evidence ->> 'size_bytes')::bigint THEN
            RETURN aap_assets_v1._managed_original_reply(
                aap_assets_v1._managed_original_facts(v_row), NULL
            );
        END IF;
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_terminal');
    END IF;
    IF v_row.status IN ('failed', 'conflict', 'unknown') THEN
        IF v_row.status = v_outcome
           AND v_row.error_code = (p_evidence ->> 'error_code') THEN
            RETURN aap_assets_v1._managed_original_reply(
                aap_assets_v1._managed_original_facts(v_row), NULL
            );
        END IF;
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_terminal');
    END IF;
    IF v_row.status <> 'allocated' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    IF v_outcome <> 'verified' THEN
        UPDATE public.managed_original_creations AS creation
        SET status = v_outcome,
            error_code = p_evidence ->> 'error_code',
            source = COALESCE(p_evidence -> 'observed', creation.source),
            completed_at = pg_catalog.clock_timestamp()
        WHERE creation.id = v_row.id
          AND creation.status = 'allocated';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
        END IF;
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object(
                'creation_id', v_row.id,
                'status', v_outcome,
                'error_code', p_evidence ->> 'error_code'
            ), NULL
        );
    END IF;

    v_sha256 := p_evidence ->> 'sha256';
    v_md5 := p_evidence ->> 'md5';
    v_size_bytes := (p_evidence ->> 'size_bytes')::bigint;
    v_etag := p_evidence ->> 'etag';
    v_version_id := NULLIF(p_evidence ->> 'version_id', 'null');
    v_request_id := NULLIF(p_evidence ->> 'request_id', 'null');
    v_verification := p_evidence -> 'verification';
    v_observed := p_evidence -> 'observed';
    v_prepared := COALESCE(
        p_evidence -> 'prepared',
        pg_catalog.jsonb_build_object(
            'mode', v_row.origin,
            'bucket', v_row.bucket,
            'object_key', v_row.object_key
        )
    );
    IF v_size_bytes < 0 OR v_size_bytes > v_row.max_bytes THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    IF pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(v_row.configuration::text, 'UTF8'), 'sha256'
        ),
        'hex'
    ) IS DISTINCT FROM v_row.input_fingerprint THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
    END IF;
    IF (v_row.configuration ->> 'mode') IS DISTINCT FROM (p_evidence ->> 'mode') THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    SELECT binding.* INTO v_binding
    FROM public.storage_bindings AS binding
    WHERE binding.purpose = 'datasets';
    IF NOT FOUND
       OR v_binding.location_id IS DISTINCT FROM v_row.location_id
       OR v_binding.bucket IS DISTINCT FROM v_row.bucket
       OR (v_row.configuration -> 'original_ref' ->> 'binding_generation')::integer
          IS DISTINCT FROM v_binding.generation THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
    END IF;
    IF (v_row.configuration ->> 'connection_revision') IS NOT NULL THEN
        SELECT connection.* INTO v_connection
        FROM public.storage_connections AS connection
        WHERE connection.id = (v_row.configuration -> 'location' ->> 'connection_id')::uuid;
        IF NOT FOUND
           OR v_connection.revision
              IS DISTINCT FROM (v_row.configuration ->> 'connection_revision')::integer THEN
            RETURN aap_assets_v1._managed_original_reply(
                NULL, 'creation_configuration_changed'
            );
        END IF;
    END IF;
    BEGIN
        v_revision := aap_assets_v1._managed_original_gate_revision(v_row.location_id);
    EXCEPTION WHEN SQLSTATE '55006' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END;
    IF v_revision IS DISTINCT FROM v_row.maintenance_revision THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
    END IF;
    v_operation_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.storage_operations (
        id, location_id, revision, purpose, node_id
    ) VALUES (
        v_operation_id, v_row.location_id, v_revision,
        'managed_original_creation', 'aap-assets-owner'
    );
    v_receipt := pg_catalog.jsonb_build_object(
        'etag', v_etag,
        'version_id', v_version_id,
        'request_id', v_request_id,
        'response', pg_catalog.jsonb_build_object(
            'ETag', v_etag,
            'VersionId', v_version_id
        )
    );

    UPDATE public.managed_original_creations AS creation
    SET status = 'preparing', claimed_at = pg_catalog.clock_timestamp()
    WHERE creation.id = v_row.id AND creation.status = 'allocated';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    UPDATE public.managed_original_creations AS creation
    SET status = 'prepared',
        operation_id = v_operation_id,
        frozen_at = pg_catalog.clock_timestamp(),
        sha256 = v_sha256,
        md5 = v_md5,
        size_bytes = v_size_bytes,
        prepared = v_prepared
    WHERE creation.id = v_row.id AND creation.status = 'preparing';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    UPDATE public.managed_original_creations AS creation
    SET status = 'writing', write_started_at = pg_catalog.clock_timestamp()
    WHERE creation.id = v_row.id AND creation.status = 'prepared';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    UPDATE public.managed_original_creations AS creation
    SET status = 'written',
        acknowledged_at = pg_catalog.clock_timestamp(),
        receipt = v_receipt,
        etag = v_etag,
        version_id = v_version_id,
        request_id = v_request_id,
        source = COALESCE(v_observed, creation.source)
    WHERE creation.id = v_row.id AND creation.status = 'writing';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    UPDATE public.managed_original_creations AS creation
    SET status = 'verified',
        verification = v_verification,
        readback_at = pg_catalog.clock_timestamp(),
        settled_at = pg_catalog.clock_timestamp(),
        completed_at = pg_catalog.clock_timestamp()
    WHERE creation.id = v_row.id AND creation.status = 'written'
    RETURNING * INTO v_row;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        aap_assets_v1._managed_original_facts(v_row), NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_managed_original_proof(uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.publish_verified_dataset_original()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_ref jsonb;
    v_asset_id uuid;
    v_state text;
    v_value text;
    v_size bigint;
BEGIN
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.owner_kind = 'dataset'
      AND creation.dataset_id = NEW.dataset_id
      AND creation.authority ->> 'item_id' = NEW.id::text
    ORDER BY creation.allocated_at DESC, creation.id DESC
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    IF v_creation.status <> 'verified' THEN
        RAISE EXCEPTION 'Managed original is not verified'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_creation_link';
    END IF;
    IF NEW.file_size IS NOT NULL
       AND NEW.file_size IS DISTINCT FROM v_creation.size_bytes THEN
        RAISE EXCEPTION 'Managed original size does not match its item'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_creation_link';
    END IF;
    IF NEW.content_hash IS NOT NULL
       AND pg_catalog.lower(NEW.content_hash)
           NOT IN (v_creation.md5, v_creation.sha256) THEN
        RAISE EXCEPTION 'Managed original digest does not match its item'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_creation_link';
    END IF;
    v_ref := v_creation.configuration -> 'original_ref';
    IF v_ref IS NULL OR pg_catalog.jsonb_typeof(v_ref) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Managed original is missing its storage reference'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_creation_link';
    END IF;
    SELECT asset.id, asset.verification_state, asset.checksum_value, asset.size_bytes
    INTO v_asset_id, v_state, v_value, v_size
    FROM public.dataset_assets AS asset
    WHERE asset.dataset_item_id = NEW.id
    FOR UPDATE;
    IF FOUND THEN
        IF v_state = 'verified'
           AND (v_value IS DISTINCT FROM v_creation.sha256
                OR v_size IS DISTINCT FROM v_creation.size_bytes) THEN
            RAISE EXCEPTION 'Dataset content differs from its verified identity'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_dataset_assets_verification';
        END IF;
        UPDATE public.dataset_assets AS asset
        SET media_type = NEW.file_type,
            size_bytes = v_creation.size_bytes,
            checksum_algorithm = 'sha256',
            checksum_value = v_creation.sha256,
            verification_state = 'verified'
        WHERE asset.id = v_asset_id;
    ELSE
        INSERT INTO public.dataset_assets (
            id, dataset_item_id, media_type, size_bytes,
            checksum_algorithm, checksum_value, verification_state
        ) VALUES (
            pg_catalog.gen_random_uuid(), NEW.id, NEW.file_type,
            v_creation.size_bytes, 'sha256', v_creation.sha256, 'verified'
        ) RETURNING id INTO v_asset_id;
    END IF;
    UPDATE public.dataset_asset_locations AS replica
    SET creation_id = v_creation.id,
        storage_ref = v_ref,
        etag = v_creation.etag,
        reference_state = 'present',
        verified_at = pg_catalog.clock_timestamp()
    WHERE replica.asset_id = v_asset_id
      AND replica.location_id = v_creation.location_id
      AND replica.bucket = v_creation.bucket
      AND replica.object_key = v_creation.object_key
      AND replica.role = 'managed_original'
      AND replica.creation_id IS NULL;
    IF NOT FOUND THEN
        INSERT INTO public.dataset_asset_locations (
            asset_id, location_id, bucket, object_key, role,
            reference_state, creation_id, storage_ref, etag, verified_at
        ) VALUES (
            v_asset_id, v_creation.location_id, v_creation.bucket,
            v_creation.object_key, 'managed_original', 'present',
            v_creation.id, v_ref, v_creation.etag, pg_catalog.clock_timestamp()
        )
        ON CONFLICT (asset_id, location_id, bucket, object_key) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.publish_verified_dataset_original() FROM PUBLIC;
-- aap-statement-break

DROP TRIGGER IF EXISTS publish_verified_dataset_original ON public.dataset_items;
-- aap-statement-break
CREATE TRIGGER publish_verified_dataset_original
AFTER INSERT ON public.dataset_items
FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.publish_verified_dataset_original();
-- aap-statement-break

CREATE INDEX IF NOT EXISTS ix_managed_creation_dataset_object
ON public.managed_original_creations (dataset_id, object_key);
-- aap-statement-break

-- Source acceptance must observe the same maintenance gate as the retired
-- worker. A stopped/draining location cannot accept or begin connector I/O.
CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_source_gate_active(
    p_connection_id uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_state text;
BEGIN
    IF p_connection_id IS NULL THEN
        RETURN false;
    END IF;
    SELECT gate.state INTO v_state
    FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id = aap_assets_v1._connector_location_id(p_connection_id);
    IF NOT FOUND THEN
        RETURN true;
    END IF;
    RETURN v_state = 'active';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_source_gate_active(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.prepare_dataset_import(
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
    IF NOT aap_assets_v1._managed_original_source_gate_active(p_connection_id) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'storage_maintenance');
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


-- Connection tests observe the same maintenance gate as source I/O.
CREATE OR REPLACE FUNCTION aap_assets_v1.record_storage_connection_test(
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
    IF NOT aap_assets_v1._managed_original_source_gate_active(connection_id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_maintenance');
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
-- aap-statement-break

-- Connection use (tests and imports) must observe the maintenance gate before
-- any probe or source I/O starts.
CREATE OR REPLACE FUNCTION aap_assets_v1.capture_storage_connection(
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
    IF NOT aap_assets_v1._managed_original_source_gate_active(connection_id) THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'storage_maintenance');
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
