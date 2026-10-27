-- Route authenticated avatar publication through the Identity owner.
--
-- 0238 owns the immutable account-command function contract. This forward
-- artifact replaces only `apply_account_command(jsonb)` so that one owner
-- transaction verifies the upload receipt and the frozen previous reference,
-- then atomically publishes `users.avatar_ref`, marks the verified pending
-- avatar ready, and retires the previous owned upload row. Physical object
-- deletion remains the durable `avatar_uploads.cleanup` responsibility.
--
-- The SECURITY DEFINER owner must be able to SELECT and UPDATE
-- public.avatar_uploads (automatic when the migration role owns both), and
-- EXECUTE aap_assets_v1.lock_publication_storage_gate(uuid, bigint). Install that
-- Assets-owned forward artifact first; this function calls it inside the same
-- transaction and holds its FOR SHARE admission lock until commit.
CREATE OR REPLACE FUNCTION aap_identity_accounts_v1.apply_account_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_field_count integer;
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_expected_key_hash text;
    v_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_base jsonb;
    v_new_value jsonb;
    v_response_preferences jsonb;
    v_actor_id uuid;
    v_lock_mode text;
    v_fence jsonb;
    v_principal jsonb;
    v_request_scope jsonb;
    v_receipt_actor_id uuid;
    v_receipt_operation text;
    v_receipt_scope jsonb;
    v_receipt_result jsonb;
    v_receipt_found boolean := false;
    v_user_name text;
    v_user_email text;
    v_user_role text;
    v_user_password_hash text;
    v_user_avatar_ref text;
    v_user_preferences jsonb;
    v_old_name text;
    v_new_name text;
    v_new_password_hash text;
    v_new_avatar_ref text;
    v_avatar_kind text;
    v_avatar_bytes bigint;
    v_expected_avatar_given boolean;
    v_upload_token text;
    v_receipt jsonb;
    v_upload_owner uuid;
    v_upload_sha256 text;
    v_upload_size bigint;
    v_upload_version text;
    v_upload_storage_ref jsonb;
    v_upload_state text;
    v_maintenance_revision bigint;
    v_location_id uuid;
    v_gate jsonb;
    v_changed boolean;
    v_account jsonb;
    v_data jsonb;
    v_audit_actor jsonb;
    v_audit_event text;
    v_audit_detail jsonb;
    v_audit_id bigint;
    v_now timestamptz;
    v_constraint_name text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 11
       OR NOT (p_request ?& ARRAY[
           'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
           'request_fingerprint', 'derivation_id', 'audit_context', 'base_value',
           'new_value', 'response_preferences'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
               'request_fingerprint', 'derivation_id', 'audit_context', 'base_value',
               'new_value', 'response_preferences'
           )
       )
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' <> 'aap-identity-accounts-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR p_request->>'operation' NOT IN (
           'read_self_account', 'get_preferences', 'heartbeat', 'update_name',
           'patch_preferences', 'change_password', 'set_avatar_ref'
       )
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'actor') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_request->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'audit_context') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    v_operation := p_request->>'operation';
    BEGIN
        v_operation_id := (p_request->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END;
    v_actor := p_request->'actor';
    v_expected_key_hash := p_request->>'expected_key_hash';
    v_fingerprint := p_request->>'request_fingerprint';
    v_derivation_id := p_request->>'derivation_id';
    v_context := p_request->'audit_context';
    v_base := p_request->'base_value';
    v_new_value := p_request->'new_value';
    v_response_preferences := p_request->'response_preferences';

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_context);
    IF v_field_count <> 4
       OR NOT (v_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_each(v_context) AS field(key, value)
           WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
       )
       OR NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    IF v_operation NOT IN (
        'heartbeat', 'update_name', 'patch_preferences', 'change_password',
        'set_avatar_ref'
    ) THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    IF v_operation = 'heartbeat' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSIF v_operation = 'update_name' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_base #>> '{}') > 100
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_new_value #>> '{}') NOT BETWEEN 1 AND 100
           OR (v_new_value #>> '{}') <> pg_catalog.btrim(v_new_value #>> '{}')
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSIF v_operation = 'patch_preferences' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'object' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSIF v_operation = 'change_password' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'string'
           OR pg_catalog.octet_length(v_base #>> '{}') NOT BETWEEN 1 AND 255
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'string'
           OR (v_new_value #>> '{}') COLLATE "C" !~ '^\$2[aby]\$(0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}$'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(v_base) NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        IF NOT (v_new_value ?& ARRAY['avatar_ref', 'kind', 'bytes'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_new_value) AS field(name)
               WHERE field.name NOT IN (
                   'avatar_ref', 'kind', 'bytes', 'expected_previous_avatar_ref',
                   'receipt'
               )
           )
           OR pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_new_value->'kind') IS DISTINCT FROM 'string'
           OR v_new_value->>'kind' NOT IN ('upload', 'preset_or_clear')
           OR pg_catalog.jsonb_typeof(v_new_value->'bytes') NOT IN ('null', 'number')
           OR pg_catalog.char_length(v_new_value->>'avatar_ref') > 80
           OR pg_catalog.char_length(v_new_value->>'expected_previous_avatar_ref') > 80 THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        v_expected_avatar_given := v_new_value ? 'expected_previous_avatar_ref';
        IF v_expected_avatar_given THEN
            IF pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                   NOT IN ('null', 'string')
               OR (
                   pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                       = 'string'
                   AND (v_new_value->>'expected_previous_avatar_ref') COLLATE "C"
                       !~ '^(preset:[a-z0-9-]{1,40}|upload:[0-9a-f]{32})$'
               ) THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
        END IF;
        v_avatar_kind := v_new_value->>'kind';
        v_new_avatar_ref := v_new_value->>'avatar_ref';
        IF v_avatar_kind = 'upload' THEN
            IF pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') IS DISTINCT FROM 'string'
               OR (v_new_value->>'avatar_ref') COLLATE "C" !~ '^upload:[0-9a-f]{32}$'
               OR pg_catalog.jsonb_typeof(v_new_value->'bytes') IS DISTINCT FROM 'number'
               OR (v_new_value->>'bytes') !~ '^(0|[1-9][0-9]*)$'
               OR NOT v_expected_avatar_given
               OR pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                   IS DISTINCT FROM COALESCE(
                       pg_catalog.jsonb_typeof(v_base), 'null'::text
                   )
               OR v_base IS DISTINCT FROM v_new_value->'expected_previous_avatar_ref'
               OR pg_catalog.jsonb_typeof(v_new_value->'receipt') IS DISTINCT FROM 'object' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            BEGIN
                v_avatar_bytes := (v_new_value->>'bytes')::bigint;
            EXCEPTION WHEN numeric_value_out_of_range THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END;
            v_receipt := v_new_value->'receipt';
            v_upload_token := v_receipt->>'token';
            SELECT pg_catalog.count(*)::integer INTO v_field_count
            FROM pg_catalog.jsonb_object_keys(v_receipt);
            IF v_field_count <> 6
               OR NOT (v_receipt ?& ARRAY[
                   'token', 'sha256', 'size_bytes', 'object_version_id',
                   'maintenance_revision', 'storage_ref'
               ])
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_object_keys(v_receipt) AS field(name)
                   WHERE field.name NOT IN (
                       'token', 'sha256', 'size_bytes', 'object_version_id',
                       'maintenance_revision', 'storage_ref'
                   )
               )
               OR pg_catalog.jsonb_typeof(v_receipt->'token') IS DISTINCT FROM 'string'
               OR (v_receipt->>'token') COLLATE "C" !~ '^[0-9a-f]{32}$'
               OR v_upload_token IS DISTINCT FROM pg_catalog.substr(v_new_avatar_ref, 8)
               OR pg_catalog.jsonb_typeof(v_receipt->'sha256') IS DISTINCT FROM 'string'
               OR (v_receipt->>'sha256') COLLATE "C" !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(v_receipt->'size_bytes') IS DISTINCT FROM 'number'
               OR (v_receipt->>'size_bytes') !~ '^(0|[1-9][0-9]*)$'
               OR pg_catalog.char_length(v_receipt->>'size_bytes') > 7
               OR (v_receipt->>'size_bytes')::bigint <> v_avatar_bytes
               OR pg_catalog.jsonb_typeof(v_receipt->'object_version_id')
                   NOT IN ('null', 'string')
               OR pg_catalog.char_length(v_receipt->>'object_version_id') > 1024
               OR pg_catalog.jsonb_typeof(v_receipt->'maintenance_revision')
                   IS DISTINCT FROM 'number'
               OR (v_receipt->>'maintenance_revision') !~ '^[1-9][0-9]{0,9}$'
               OR pg_catalog.jsonb_typeof(v_receipt->'storage_ref') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_receipt->'storage_ref'->'location_id')
                   IS DISTINCT FROM 'string'
               OR (v_receipt->'storage_ref'->>'location_id') COLLATE "C"
                   !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            v_maintenance_revision := (v_receipt->>'maintenance_revision')::bigint;
            v_location_id := (v_receipt->'storage_ref'->>'location_id')::uuid;
        ELSE
            IF (
                v_new_value ? 'receipt'
                AND pg_catalog.jsonb_typeof(v_new_value->'receipt')
                    IS DISTINCT FROM 'null'
            )
               OR pg_catalog.jsonb_typeof(v_new_value->'bytes') IS DISTINCT FROM 'null'
               OR (pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') = 'string'
                   AND (v_new_value->>'avatar_ref') COLLATE "C"
                       !~ '^preset:[a-z0-9-]{1,40}$') THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            IF v_expected_avatar_given
               AND pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                   = 'string'
               AND v_base IS DISTINCT FROM
                   v_new_value->'expected_previous_avatar_ref' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
        END IF;
    END IF;

    v_lock_mode := CASE v_operation
        WHEN 'patch_preferences' THEN 'write_wait'
        WHEN 'heartbeat' THEN 'write_nowait'
        ELSE 'write_nowait'
    END;
    v_fence := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, v_lock_mode
    );
    IF v_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_fence;
    END IF;
    v_principal := v_fence->'data';
    v_actor_id := (v_principal->>'actor_id')::uuid;

    SELECT account.name, account.email, account.role, account.password_hash,
           account.avatar_ref, account.preferences
    INTO v_user_name, v_user_email, v_user_role, v_user_password_hash,
         v_user_avatar_ref, v_user_preferences
    FROM public.users AS account
    WHERE account.id = v_actor_id;
    IF NOT FOUND THEN
        RETURN aap_identity_accounts_v1._failure('credential_invalid', NULL, NULL);
    END IF;

    IF v_operation = 'heartbeat' THEN
        v_now := pg_catalog.clock_timestamp();
        UPDATE public.users AS account
        SET last_seen_at = v_now, status = 'online'
        WHERE account.id = v_actor_id;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-accounts-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', pg_catalog.jsonb_build_object(
                'account', NULL, 'preferences', NULL,
                'previous_avatar_ref', NULL, 'changed', NULL
            )
        );
    END IF;

    v_request_scope := pg_catalog.jsonb_build_object(
        'actor_id', v_actor_id,
        'operation', v_operation,
        'request_fingerprint', v_fingerprint,
        'derivation_id', v_derivation_id
    );
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-account-operation/' || v_operation_id::text, 0
        )
    ) THEN
        RETURN aap_identity_accounts_v1._failure('credential_busy', NULL, NULL);
    END IF;
    SELECT receipt.actor_id, receipt.operation, receipt.request_scope, receipt.result
    INTO v_receipt_actor_id, v_receipt_operation, v_receipt_scope, v_receipt_result
    FROM public.identity_account_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt_actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt_operation IS DISTINCT FROM v_operation
           OR v_receipt_scope IS DISTINCT FROM v_request_scope THEN
            RETURN aap_identity_accounts_v1._failure('operation_conflict', NULL, NULL);
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-accounts-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_receipt_result
        );
    END IF;

    BEGIN
        IF v_operation = 'update_name' THEN
            v_old_name := v_user_name;
            v_new_name := v_new_value #>> '{}';
            IF (to_jsonb(v_old_name) IS DISTINCT FROM v_base) THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            v_changed := v_old_name IS DISTINCT FROM v_new_name;
            IF v_changed THEN
                UPDATE public.users AS account
                SET name = v_new_name
                WHERE account.id = v_actor_id;
                v_principal := v_principal || pg_catalog.jsonb_build_object(
                    'name', v_new_name
                );
                v_audit_actor := pg_catalog.jsonb_build_object(
                    'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
                );
                v_audit_event := 'update_name';
                v_audit_detail := pg_catalog.jsonb_build_object(
                    'old_name', v_old_name, 'new_name', v_new_name
                );
                v_audit_id := aap_audit_v1.record_identity_account_event(
                    v_audit_event, v_audit_actor, v_context, v_audit_detail
                );
            END IF;
            v_account := aap_identity_accounts_v1._account_record(v_actor_id);
            v_data := pg_catalog.jsonb_build_object(
                'account', v_account,
                'preferences', NULL,
                'previous_avatar_ref', NULL,
                'changed', v_changed
            );
        ELSIF v_operation = 'patch_preferences' THEN
            IF v_user_preferences IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            UPDATE public.users AS account
            SET preferences = v_new_value
            WHERE account.id = v_actor_id;
            v_data := pg_catalog.jsonb_build_object(
                'account', NULL,
                'preferences', v_response_preferences,
                'previous_avatar_ref', NULL,
                'changed', NULL
            );
        ELSIF v_operation = 'change_password' THEN
            IF to_jsonb(v_user_password_hash) IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            v_new_password_hash := v_new_value #>> '{}';
            UPDATE public.users AS account
            SET password_hash = v_new_password_hash,
                password_admin_reset_at = NULL
            WHERE account.id = v_actor_id;
            v_audit_actor := pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
            );
            v_audit_event := 'change_password';
            v_audit_detail := pg_catalog.jsonb_build_object('email', v_user_email);
            v_audit_id := aap_audit_v1.record_identity_account_event(
                v_audit_event, v_audit_actor, v_context, v_audit_detail
            );
            v_data := pg_catalog.jsonb_build_object(
                'account', NULL,
                'preferences', NULL,
                'previous_avatar_ref', NULL,
                'changed', NULL
            );
        ELSE
            IF (CASE
                    WHEN v_user_avatar_ref IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(v_user_avatar_ref)
                END) IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure(
                    CASE
                        WHEN v_avatar_kind = 'upload'
                        THEN 'avatar_reference_changed'
                        ELSE 'account_busy'
                    END,
                    NULL, NULL
                );
            END IF;
            v_changed := v_user_avatar_ref IS DISTINCT FROM v_new_avatar_ref;
            IF v_avatar_kind = 'upload' THEN
                -- Reacquire the Asset-owned admission gate at the exact revision
                -- captured by object I/O. The FOR SHARE lock is held until this
                -- owner transaction commits, so a maintenance transition cannot
                -- slip between admission and users.avatar_ref publication.
                v_gate := aap_assets_v1.lock_publication_storage_gate(
                    v_location_id, v_maintenance_revision
                );
                IF v_gate->>'ok' IS DISTINCT FROM 'true' THEN
                    RETURN aap_identity_accounts_v1._failure(
                        v_gate->>'code', NULL, NULL
                    );
                END IF;
                SELECT upload.owner_id, upload.sha256, upload.size_bytes,
                       upload.object_version_id, upload.storage_ref, upload.state
                INTO v_upload_owner, v_upload_sha256, v_upload_size,
                     v_upload_version, v_upload_storage_ref, v_upload_state
                FROM public.avatar_uploads AS upload
                WHERE upload.token = v_upload_token
                FOR UPDATE;
                IF NOT FOUND
                   OR v_upload_owner IS DISTINCT FROM v_actor_id
                   OR v_upload_state IS DISTINCT FROM 'pending'
                   OR v_upload_sha256 IS DISTINCT FROM (v_receipt->>'sha256')
                   OR v_upload_size IS DISTINCT FROM v_avatar_bytes
                   OR v_upload_version IS DISTINCT FROM (
                       CASE
                           WHEN pg_catalog.jsonb_typeof(
                               v_receipt->'object_version_id'
                           ) = 'null' THEN NULL
                           ELSE v_receipt->>'object_version_id'
                       END
                   )
                   OR v_upload_storage_ref IS DISTINCT FROM v_receipt->'storage_ref'
                   OR NOT v_changed THEN
                    RETURN aap_identity_accounts_v1._failure(
                        'avatar_receipt_invalid', NULL, NULL
                    );
                END IF;
                UPDATE public.avatar_uploads AS upload
                SET state = 'ready'
                WHERE upload.token = v_upload_token
                  AND upload.owner_id = v_actor_id
                  AND upload.state = 'pending';
            END IF;
            IF v_changed THEN
                IF v_user_avatar_ref IS NOT NULL
                   AND v_user_avatar_ref COLLATE "C"
                       ~ '^upload:[0-9a-f]{32}$' THEN
                    UPDATE public.avatar_uploads AS upload
                    SET state = 'retired', retired_at = pg_catalog.clock_timestamp()
                    WHERE upload.token = pg_catalog.substr(v_user_avatar_ref, 8)
                      AND upload.owner_id = v_actor_id
                      AND upload.state = 'ready';
                END IF;
                UPDATE public.users AS account
                SET avatar_ref = v_new_avatar_ref
                WHERE account.id = v_actor_id;
                v_audit_actor := pg_catalog.jsonb_build_object(
                    'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
                );
                v_audit_event := 'set_avatar_ref';
                IF v_avatar_kind = 'upload' THEN
                    v_audit_detail := pg_catalog.jsonb_build_object(
                        'field', 'avatar', 'avatar_ref', v_new_avatar_ref,
                        'kind', v_avatar_kind, 'bytes', v_avatar_bytes
                    );
                ELSE
                    v_audit_detail := pg_catalog.jsonb_build_object(
                        'field', 'avatar', 'avatar_ref', v_new_avatar_ref,
                        'kind', v_avatar_kind
                    );
                END IF;
                v_audit_id := aap_audit_v1.record_identity_account_event(
                    v_audit_event, v_audit_actor, v_context, v_audit_detail
                );
            END IF;
            v_account := aap_identity_accounts_v1._account_record(v_actor_id);
            v_data := pg_catalog.jsonb_build_object(
                'account', v_account,
                'preferences', NULL,
                'previous_avatar_ref', CASE
                    WHEN v_changed THEN v_user_avatar_ref ELSE NULL::text
                END,
                'changed', v_changed
            );
        END IF;

        INSERT INTO public.identity_account_operation_receipts (
            operation_id, actor_id, operation, request_scope, result, audit_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_request_scope, v_data, v_audit_id
        );
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
        IF v_constraint_name = 'identity_account_operation_receipts_pkey' THEN
            RETURN aap_identity_accounts_v1._failure('operation_conflict', NULL, NULL);
        END IF;
        RAISE;
    END;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-accounts-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1.apply_account_command(jsonb)
    FROM PUBLIC;
