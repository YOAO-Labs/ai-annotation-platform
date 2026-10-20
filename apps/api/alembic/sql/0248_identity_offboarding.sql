-- Native Identity offboarding preview and atomic handoff commands.
CREATE FUNCTION aap_identity_offboarding_v1._failure(
    p_code text,
    p_status_code integer DEFAULT NULL,
    p_detail jsonb DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-offboarding-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code,
            'status_code', p_status_code,
            'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_offboarding_v1._failure(
    text, integer, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_offboarding_v1._has_exact_keys(
    p_value jsonb,
    p_keys text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN false
        ELSE (SELECT pg_catalog.count(*)
              FROM pg_catalog.jsonb_object_keys(p_value))
                 = pg_catalog.cardinality(p_keys)
             AND p_value ?& p_keys
             AND NOT EXISTS (
                 SELECT 1 FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
                 WHERE NOT (field.name = ANY(p_keys))
             )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_offboarding_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_offboarding_v1.offboarding_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-offboarding-policy/v1',
        'policy', 'aap-identity-offboarding/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_offboarding_v1.offboarding_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_offboarding_v1.prepare_offboarding_command(
    p_command jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_actor_id uuid;
    v_expected_key_hash text;
    v_request_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_payload jsonb;
    v_target_user_id uuid;
    v_receiver_ids uuid[] := ARRAY[]::uuid[];
    v_payload_project_ids uuid[] := ARRAY[]::uuid[];
    v_json_item jsonb;
    v_item_id uuid;
    v_previous_id uuid;
    v_locked_user_id uuid;
    v_target_found boolean := false;
    v_target_role text;
    v_target_active boolean;
    v_target_generation bigint;
    v_principal_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_workspace_lock_ids uuid[] := ARRAY[]::uuid[];
    v_labeling_locks_acquired boolean;
    v_workspace_facts record;
    v_labeling_facts record;
    v_target_record jsonb;
    v_api_keys jsonb;
    v_candidate_users jsonb;
    v_candidate_memberships jsonb;
    v_super_admin_status jsonb;
    v_other_unreserved bigint;
    v_facts jsonb;
    v_data jsonb;
    v_receipt record;
    v_receipt_found boolean := false;
    v_scope_basis jsonb;
    v_scope_status jsonb := pg_catalog.jsonb_build_object(
        'projects_exist', true,
        'all_projects_owned', true
    );
    v_scope_reply record;
    v_scope_project_ids uuid[] := ARRAY[]::uuid[];
    v_context_binding jsonb;
    v_expected_count integer;
    v_seen_count integer := 0;
    v_project jsonb;
    v_project_id uuid;
BEGIN
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'phase', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-offboarding-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'phase') IS DISTINCT FROM 'string'
       OR p_command->>'phase' IS DISTINCT FROM 'prepare'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR p_command->>'operation' NOT IN (
           'preview_offboarding', 'commit_offboarding'
       )
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(
           pg_catalog.jsonb_typeof(p_command->'expected_key_hash')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active',
               'credential_type', 'credential_id', 'generation', 'scopes',
               'expires_at'
           ]
       )
       OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}') IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           p_command->'audit_context',
           ARRAY['method', 'path', 'ip', 'request_id']
       )
       OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation := p_command->>'operation';
    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_request_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';

    IF v_operation = 'preview_offboarding' THEN
        IF NOT aap_identity_offboarding_v1._has_exact_keys(
            v_payload, ARRAY['target_user_id']
        ) OR pg_catalog.jsonb_typeof(v_payload->'target_user_id') IS DISTINCT FROM 'string'
          OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
    ELSE
        IF NOT aap_identity_offboarding_v1._has_exact_keys(
            v_payload, ARRAY['target_user_id', 'receiver_ids', 'payload_project_ids']
        ) OR pg_catalog.jsonb_typeof(v_payload->'target_user_id') IS DISTINCT FROM 'string'
          OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          OR pg_catalog.jsonb_typeof(v_payload->'receiver_ids') IS DISTINCT FROM 'array'
          OR pg_catalog.jsonb_typeof(v_payload->'payload_project_ids') IS DISTINCT FROM 'array' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;

        v_previous_id := NULL;
        FOR v_json_item IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_payload->'receiver_ids') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
               OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
            v_item_id := (v_json_item #>> '{}')::uuid;
            IF v_previous_id IS NOT NULL AND v_item_id <= v_previous_id THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
            v_receiver_ids := pg_catalog.array_append(v_receiver_ids, v_item_id);
            v_previous_id := v_item_id;
        END LOOP;

        v_previous_id := NULL;
        FOR v_json_item IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_payload->'payload_project_ids') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
               OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
            v_item_id := (v_json_item #>> '{}')::uuid;
            IF v_previous_id IS NOT NULL AND v_item_id <= v_previous_id THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
            v_payload_project_ids := pg_catalog.array_append(
                v_payload_project_ids, v_item_id
            );
            v_previous_id := v_item_id;
        END LOOP;
    END IF;
    v_target_user_id := (v_payload->>'target_user_id')::uuid;

    IF v_operation = 'commit_offboarding' THEN
        IF NULLIF(
            pg_catalog.current_setting('aap.identity_offboarding_prepare', true), ''
        ) IS NOT NULL THEN
            RETURN aap_identity_offboarding_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
            RETURN aap_identity_offboarding_v1._failure(
                'account_busy', 409,
                pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
            );
        END IF;

        BEGIN
            FOR v_locked_user_id IN
                SELECT account.id
                FROM public.users AS account
                WHERE account.id = ANY(
                    ARRAY[v_actor_id, v_target_user_id]::uuid[] || v_receiver_ids
                )
                ORDER BY account.id
                FOR UPDATE OF account NOWAIT
            LOOP
                NULL;
            END LOOP;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_offboarding_v1._failure(
                'account_busy', 409,
                pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
            );
        END;
    END IF;

    SELECT account.role, account.is_active, account.credential_generation
    INTO v_target_role, v_target_active, v_target_generation
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    v_target_found := FOUND;

    IF v_operation = 'commit_offboarding' THEN
        IF NOT v_target_found THEN
            SELECT receipt.actor_id, receipt.operation, receipt.target_user_id,
                   receipt.request_fingerprint, receipt.derivation_id,
                   receipt.target_generation, receipt.target_is_active,
                   receipt.result, receipt.scope_basis
            INTO v_receipt
            FROM public.identity_offboarding_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            v_receipt_found := FOUND;
            IF NOT v_receipt_found THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
                );
            END IF;
        ELSE
            IF NOT pg_catalog.pg_try_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'aap:identity-offboarding-operation/v1:' || v_operation_id::text,
                    0
                )
            ) THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            END IF;
            SELECT receipt.actor_id, receipt.operation, receipt.target_user_id,
                   receipt.request_fingerprint, receipt.derivation_id,
                   receipt.target_generation, receipt.target_is_active,
                   receipt.result, receipt.scope_basis
            INTO v_receipt
            FROM public.identity_offboarding_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            v_receipt_found := FOUND;
        END IF;
        IF NOT v_target_found THEN
            v_principal_reply := aap_identity_accounts_v1._lock_current_actor(
                v_actor, v_expected_key_hash, 'write_nowait'
            );
            IF v_principal_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                CASE v_principal_reply #>> '{error,code}'
                    WHEN 'user_inactive' THEN
                        RETURN aap_identity_offboarding_v1._failure(
                            'manager_required', 403,
                            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
                        );
                    WHEN 'account_busy' THEN
                        RETURN aap_identity_offboarding_v1._failure(
                            'account_busy', 409,
                            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                        );
                    WHEN 'invalid_request' THEN
                        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
                    WHEN 'credential_invalid' THEN
                        RETURN aap_identity_offboarding_v1._failure('credential_invalid', NULL, NULL);
                    WHEN 'credential_revoked' THEN
                        RETURN aap_identity_offboarding_v1._failure('credential_revoked', NULL, NULL);
                    WHEN 'generation_outdated' THEN
                        RETURN aap_identity_offboarding_v1._failure('generation_outdated', NULL, NULL);
                    WHEN 'token_expired' THEN
                        RETURN aap_identity_offboarding_v1._failure('token_expired', NULL, NULL);
                    WHEN 'invalid_account_role' THEN
                        RETURN aap_identity_offboarding_v1._failure('invalid_account_role', NULL, NULL);
                    WHEN 'credential_busy' THEN
                        RETURN aap_identity_offboarding_v1._failure('credential_busy', NULL, NULL);
                    ELSE
                        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
                END CASE;
            END IF;
            v_principal := v_principal_reply->'data';
            IF v_principal->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'manager_required', 403,
                    pg_catalog.to_jsonb('当前账号已无管理权限'::text)
                );
            END IF;
            IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.operation IS DISTINCT FROM 'commit_offboarding'
               OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'operation_conflict', 409, NULL
                );
            END IF;
            RETURN aap_identity_offboarding_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;

        v_principal_reply := aap_identity_accounts_v1._lock_current_actor(
            v_actor, v_expected_key_hash, 'write_nowait'
        );
    ELSE
        IF NOT v_target_found THEN
            RETURN aap_identity_offboarding_v1._failure(
                'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
            );
        END IF;
        v_principal_reply := aap_identity_accounts_v1._lock_current_actor(
            v_actor, v_expected_key_hash, 'read'
        );
    END IF;

    IF v_principal_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_principal_reply #>> '{error,code}'
            WHEN 'user_inactive' THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'manager_required', 403,
                    pg_catalog.to_jsonb('当前账号已无管理权限'::text)
                );
            WHEN 'account_busy' THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            WHEN 'invalid_request' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_offboarding_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_offboarding_v1._failure('token_expired', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_busy', NULL, NULL);
            ELSE
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_principal_reply->'data';
    v_actor_role := v_principal->>'role';

    IF v_operation = 'commit_offboarding' AND v_receipt_found THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.operation IS DISTINCT FROM 'commit_offboarding'
           OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_offboarding_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        IF v_receipt.target_generation IS DISTINCT FROM v_target_generation
           OR v_receipt.target_is_active IS DISTINCT FROM v_target_active THEN
            RETURN aap_identity_offboarding_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
            RETURN aap_identity_offboarding_v1._failure(
                'manager_required', 403,
                pg_catalog.to_jsonb('当前账号已无管理权限'::text)
            );
        END IF;
        v_scope_basis := v_receipt.scope_basis;
        IF NOT aap_identity_offboarding_v1._has_exact_keys(
            v_scope_basis, ARRAY['target_role', 'target_was_active', 'project_ids']
        ) OR pg_catalog.jsonb_typeof(v_scope_basis->'target_role') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(v_scope_basis->'target_was_active') IS DISTINCT FROM 'boolean'
          OR pg_catalog.jsonb_typeof(v_scope_basis->'project_ids') IS DISTINCT FROM 'array' THEN
            RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
        END IF;
        v_scope_project_ids := ARRAY[]::uuid[];
        v_previous_id := NULL;
        FOR v_json_item IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_scope_basis->'project_ids') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
               OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
            END IF;
            v_item_id := (v_json_item #>> '{}')::uuid;
            IF v_previous_id IS NOT NULL AND v_item_id <= v_previous_id THEN
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
            END IF;
            v_scope_project_ids := pg_catalog.array_append(v_scope_project_ids, v_item_id);
            v_previous_id := v_item_id;
        END LOOP;
        IF v_actor_role = 'project_admin' THEN
            IF v_scope_basis->>'target_role' IS DISTINCT FROM 'employee'
               OR (pg_catalog.cardinality(v_scope_project_ids) = 0
                   AND v_scope_basis->'target_was_active' IS DISTINCT FROM 'true'::jsonb) THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'scope_forbidden', 403,
                    pg_catalog.to_jsonb('该用户存在不在你管理范围内的项目'::text)
                );
            END IF;
            BEGIN
                SELECT scope.projects_exist, scope.all_projects_owned
                INTO STRICT v_scope_reply
                FROM aap_workspace_v1.identity_offboarding_replay_scope(
                    v_actor_id, v_scope_project_ids
                ) AS scope;
            EXCEPTION WHEN no_data_found OR too_many_rows THEN
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
            END;
            IF v_scope_reply.projects_exist IS NULL
               OR v_scope_reply.all_projects_owned IS NULL THEN
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
            END IF;
            v_scope_status := pg_catalog.jsonb_build_object(
                'projects_exist', v_scope_reply.projects_exist,
                'all_projects_owned', v_scope_reply.all_projects_owned
            );
            IF NOT v_scope_reply.projects_exist OR NOT v_scope_reply.all_projects_owned THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'scope_forbidden', 403,
                    pg_catalog.to_jsonb('该用户存在不在你管理范围内的项目'::text)
                );
            END IF;
        END IF;
        v_data := pg_catalog.jsonb_build_object(
            'kind', 'replay',
            'transaction_id', pg_catalog.txid_current()::text,
            'target_generation', v_target_generation,
            'result', v_receipt.result,
            'scope_basis', v_scope_basis,
            'scope_status', v_scope_status
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-offboarding-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'phase', 'prepare',
            'data', v_data
        );
    END IF;

    IF v_operation = 'commit_offboarding' THEN
        BEGIN
            v_workspace_lock_ids := aap_workspace_v1.lock_identity_offboarding_scope(
                v_target_user_id, v_payload_project_ids, v_receiver_ids
            );
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_offboarding_v1._failure(
                'offboarding_busy', 409,
                pg_catalog.jsonb_build_object(
                    'code', 'offboarding_busy',
                    'message', '职责正在变化，请刷新预览后重试'
                )
            );
        END;
        IF v_workspace_lock_ids IS NULL
           OR pg_catalog.array_position(v_workspace_lock_ids, NULL::uuid) IS NOT NULL THEN
            RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
        END IF;
        BEGIN
            v_labeling_locks_acquired := aap_labeling_v1.lock_identity_offboarding_work(
                v_target_user_id, v_workspace_lock_ids
            );
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_offboarding_v1._failure(
                'offboarding_busy', 409,
                pg_catalog.jsonb_build_object(
                    'code', 'offboarding_busy',
                    'message', '职责正在变化，请刷新预览后重试'
                )
            );
        END;
        IF v_labeling_locks_acquired IS NULL THEN
            RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
        END IF;
        IF NOT v_labeling_locks_acquired THEN
            RETURN aap_identity_offboarding_v1._failure(
                'offboarding_busy', 409,
                pg_catalog.jsonb_build_object('code', 'offboarding_busy')
            );
        END IF;
    END IF;

    IF NOT v_target_found THEN
        RETURN aap_identity_offboarding_v1._failure(
            'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
        );
    END IF;

    SELECT account.role, account.is_active, account.credential_generation
    INTO v_target_role, v_target_active, v_target_generation
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    IF NOT FOUND OR v_target_generation IS NULL OR v_target_generation < 0 THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
    END IF;
    v_target_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_target_record IS NULL THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
    END IF;

    BEGIN
        SELECT * INTO STRICT v_workspace_facts
        FROM aap_workspace_v1.identity_offboarding_facts(v_target_user_id);
        SELECT * INTO STRICT v_labeling_facts
        FROM aap_labeling_v1.identity_offboarding_facts(v_target_user_id);
    EXCEPTION WHEN no_data_found OR too_many_rows THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
    END;
    IF pg_catalog.jsonb_typeof(v_workspace_facts.projects) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_workspace_facts.memberships) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_workspace_facts.candidate_memberships) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_labeling_facts.batches) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_labeling_facts.tasks) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_labeling_facts.locks) IS DISTINCT FROM 'array' THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
    END IF;

    IF v_operation = 'commit_offboarding' THEN
        FOR v_project IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_workspace_facts.projects) AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_project->'id') IS DISTINCT FROM 'string'
               OR (v_project->>'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
            END IF;
            v_project_id := (v_project->>'id')::uuid;
            IF NOT (v_project_id = ANY(v_workspace_lock_ids)) THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'offboarding_busy', 409,
                    pg_catalog.jsonb_build_object(
                        'code', 'offboarding_busy',
                        'message', '职责正在变化，请刷新预览后重试'
                    )
                );
            END IF;
        END LOOP;
    END IF;

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', api_key.id,
                       'name', api_key.name,
                       'key_prefix', api_key.key_prefix,
                       'last_used_at', api_key.last_used_at,
                       'revoked_at', api_key.revoked_at,
                       'created_at', api_key.created_at
                   ) ORDER BY api_key.created_at DESC, api_key.id
               ),
               '[]'::jsonb
           )
    INTO v_api_keys
    FROM public.api_keys AS api_key
    WHERE api_key.user_id = v_target_user_id;

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', account.id,
                       'name', account.name,
                       'email', account.email,
                       'role', account.role
                   ) ORDER BY account.name, account.email, account.id
               ),
               '[]'::jsonb
           )
    INTO v_candidate_users
    FROM public.users AS account
    WHERE account.is_active IS TRUE
      AND account.id <> v_target_user_id
      AND (
          account.role = 'project_admin'
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.jsonb_array_elements(
                  v_workspace_facts.candidate_memberships
              ) AS membership(value)
              WHERE membership.value->>'user_id' = account.id::text
          )
      );

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   membership.value ORDER BY membership.ordinality
               ),
               '[]'::jsonb
           )
    INTO v_candidate_memberships
    FROM pg_catalog.jsonb_array_elements(
        v_workspace_facts.candidate_memberships
    ) WITH ORDINALITY AS membership(value, ordinality)
    WHERE EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_candidate_users) AS candidate(value)
        WHERE candidate.value->>'id' = membership.value->>'user_id'
    );

    v_super_admin_status := aap_identity_v1.super_admin_status(v_target_user_id);
    IF pg_catalog.jsonb_typeof(v_super_admin_status) IS DISTINCT FROM 'object'
       OR v_super_admin_status->>'schema' IS DISTINCT FROM
          'aap-identity-super-admin-status/v1'
       OR pg_catalog.jsonb_typeof(v_super_admin_status->'other_unreserved') IS DISTINCT FROM 'number'
       OR (v_super_admin_status->>'other_unreserved') !~ '^(0|[1-9][0-9]*)$' THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
    END IF;
    BEGIN
        v_other_unreserved := (v_super_admin_status->>'other_unreserved')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
    END;

    v_facts := pg_catalog.jsonb_build_object(
        'target', v_target_record,
        'projects', v_workspace_facts.projects,
        'members', v_workspace_facts.memberships,
        'batches', v_labeling_facts.batches,
        'tasks', v_labeling_facts.tasks,
        'locks', v_labeling_facts.locks,
        'api_keys', v_api_keys,
        'candidate_users', v_candidate_users,
        'candidate_memberships', v_candidate_memberships,
        'other_unreserved', v_other_unreserved
    );

    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;

    IF v_operation = 'commit_offboarding' THEN
        v_context_binding := pg_catalog.jsonb_build_object(
            'transaction_id', pg_catalog.txid_current()::text,
            'operation_id', v_operation_id,
            'operation', v_operation,
            'actor_id', v_actor_id,
            'target_user_id', v_target_user_id,
            'target_role', v_target_role,
            'request_fingerprint', v_request_fingerprint,
            'derivation_id', v_derivation_id,
            'target_generation', v_target_generation,
            'target_is_active', v_target_active,
            'receiver_ids', pg_catalog.to_jsonb(v_receiver_ids),
            'locked_project_ids', pg_catalog.to_jsonb(v_workspace_lock_ids)
        );
        PERFORM pg_catalog.set_config(
            'aap.identity_offboarding_prepare', v_context_binding::text, true
        );
    END IF;

    v_data := pg_catalog.jsonb_build_object(
        'kind', 'snapshot',
        'transaction_id', pg_catalog.txid_current()::text,
        'target_generation', v_target_generation,
        'target_is_active', v_target_active,
        'facts', v_facts
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-offboarding-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'phase', 'prepare',
        'data', v_data
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_offboarding_v1.prepare_offboarding_command(
    jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_offboarding_v1.apply_offboarding_command(
    p_command jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_operation_id uuid;
    v_actor jsonb;
    v_actor_id uuid;
    v_expected_key_hash text;
    v_request_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_payload jsonb;
    v_target_user_id uuid;
    v_expected_generation bigint;
    v_expected_active boolean;
    v_mode text;
    v_normalized_reason text;
    v_reason_present boolean;
    v_scope_basis jsonb;
    v_scope_project_ids uuid[] := ARRAY[]::uuid[];
    v_owner_plan jsonb;
    v_labeling_plan jsonb;
    v_transfers jsonb;
    v_unresolved jsonb;
    v_owner_plan_item jsonb;
    v_labeling_plan_item jsonb;
    v_transfer_item jsonb;
    v_json_item jsonb;
    v_item_id uuid;
    v_previous_id uuid;
    v_owner_project_ids uuid[] := ARRAY[]::uuid[];
    v_actual_owner_ids uuid[] := ARRAY[]::uuid[];
    v_receiver_ids uuid[] := ARRAY[]::uuid[];
    v_locked_project_ids uuid[] := ARRAY[]::uuid[];
    v_actor_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_target_role text;
    v_target_disabled_kind text;
    v_target_active boolean;
    v_target_generation bigint;
    v_target_found boolean := false;
    v_current_target_role text;
    v_binding_text text;
    v_binding jsonb;
    v_binding_receiver_ids uuid[] := ARRAY[]::uuid[];
    v_binding_project_ids uuid[] := ARRAY[]::uuid[];
    v_scope_reply record;
    v_super_admin_status jsonb;
    v_other_unreserved bigint;
    v_owner_result jsonb;
    v_labeling_result jsonb;
    v_label_result_item jsonb;
    v_retirement jsonb;
    v_new_generation bigint;
    v_revoked_key_ids jsonb;
    v_released_lock_count bigint;
    v_audit_id bigint;
    v_user_record jsonb;
    v_result jsonb;
    v_expected_owner_count bigint;
    v_actual_owner_count bigint;
    v_expected_label_count bigint;
    v_actual_label_count bigint;
    v_match_count bigint;
    v_count bigint;
    v_index integer := 0;
    v_rows integer;
    v_role text;
    v_project_id uuid;
    v_receiver_id uuid;
    v_context_actor jsonb;
BEGIN
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'phase', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-offboarding-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'phase') IS DISTINCT FROM 'string'
       OR p_command->>'phase' IS DISTINCT FROM 'apply'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR p_command->>'operation' IS DISTINCT FROM 'commit_offboarding'
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(
           pg_catalog.jsonb_typeof(p_command->'expected_key_hash')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active',
               'credential_type', 'credential_id', 'generation', 'scopes',
               'expires_at'
           ]
       )
       OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}') IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           p_command->'audit_context',
           ARRAY['method', 'path', 'ip', 'request_id']
       )
       OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_request_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';

    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        v_payload,
        ARRAY[
            'transaction_id', 'target_user_id', 'expected_target_generation',
            'expected_target_is_active', 'mode', 'normalized_reason',
            'reason_present', 'owner_plan', 'labeling_plan', 'transfers',
            'unresolved', 'scope_basis'
        ]
    )
       OR pg_catalog.jsonb_typeof(v_payload->'transaction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_payload->'target_user_id') IS DISTINCT FROM 'string'
       OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(v_payload->'expected_target_generation') IS DISTINCT FROM 'number'
       OR (v_payload->>'expected_target_generation') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_payload->'expected_target_is_active') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_payload->'mode') IS DISTINCT FROM 'string'
       OR v_payload->>'mode' NOT IN ('handoff', 'emergency_suspend')
       OR COALESCE(
           pg_catalog.jsonb_typeof(v_payload->'normalized_reason')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(v_payload->'normalized_reason') = 'string'
           AND (
               pg_catalog.char_length(v_payload->>'normalized_reason') > 500
               OR v_payload->>'normalized_reason' = ''
           ))
       OR pg_catalog.jsonb_typeof(v_payload->'reason_present') IS DISTINCT FROM 'boolean'
       OR ((v_payload->'reason_present' = 'true'::jsonb)
           IS DISTINCT FROM (pg_catalog.jsonb_typeof(v_payload->'normalized_reason') = 'string'))
       OR pg_catalog.jsonb_typeof(v_payload->'owner_plan') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload->'labeling_plan') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload->'transfers') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload->'unresolved') IS DISTINCT FROM 'array'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           v_payload->'scope_basis',
           ARRAY['target_role', 'target_was_active', 'project_ids']
       )
       OR pg_catalog.jsonb_typeof(v_payload #> '{scope_basis,target_role}') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_payload #> '{scope_basis,target_was_active}') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_payload #> '{scope_basis,project_ids}') IS DISTINCT FROM 'array' THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_target_user_id := (v_payload->>'target_user_id')::uuid;
    v_mode := v_payload->>'mode';
    v_reason_present := (v_payload->>'reason_present')::boolean;
    v_normalized_reason := v_payload->>'normalized_reason';
    v_scope_basis := v_payload->'scope_basis';
    v_owner_plan := v_payload->'owner_plan';
    v_labeling_plan := v_payload->'labeling_plan';
    v_transfers := v_payload->'transfers';
    v_unresolved := v_payload->'unresolved';
    BEGIN
        v_expected_generation := (v_payload->>'expected_target_generation')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END;
    v_expected_active := (v_payload->>'expected_target_is_active')::boolean;
    IF v_expected_active IS DISTINCT FROM
       ((v_scope_basis->'target_was_active') = 'true'::jsonb) THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_previous_id := NULL;
    FOR v_json_item IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(v_scope_basis->'project_ids') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_item_id := (v_json_item #>> '{}')::uuid;
        IF v_previous_id IS NOT NULL AND v_item_id <= v_previous_id THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_scope_project_ids := pg_catalog.array_append(v_scope_project_ids, v_item_id);
        v_previous_id := v_item_id;
    END LOOP;

    v_binding_text := NULLIF(
        pg_catalog.current_setting('aap.identity_offboarding_prepare', true), ''
    );
    IF v_binding_text IS NULL THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END IF;
    BEGIN
        v_binding := v_binding_text::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END;
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        v_binding,
        ARRAY[
            'transaction_id', 'operation_id', 'operation', 'actor_id',
            'target_user_id', 'target_role', 'request_fingerprint',
            'derivation_id', 'target_generation', 'target_is_active',
            'receiver_ids', 'locked_project_ids'
        ]
    )
       OR v_binding->>'transaction_id' IS DISTINCT FROM pg_catalog.txid_current()::text
       OR v_binding->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_binding->>'operation' IS DISTINCT FROM 'commit_offboarding'
       OR v_binding->>'actor_id' IS DISTINCT FROM v_actor_id::text
       OR v_binding->>'target_user_id' IS DISTINCT FROM v_target_user_id::text
       OR v_binding->>'request_fingerprint' IS DISTINCT FROM v_request_fingerprint
       OR v_binding->>'derivation_id' IS DISTINCT FROM v_derivation_id
       OR v_binding->'target_generation' IS DISTINCT FROM
          v_payload->'expected_target_generation'
       OR v_binding->'target_is_active' IS DISTINCT FROM
          v_payload->'expected_target_is_active'
       OR v_binding->>'target_role' IS DISTINCT FROM
          v_scope_basis->>'target_role'
       OR pg_catalog.jsonb_typeof(v_binding->'receiver_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_binding->'locked_project_ids') IS DISTINCT FROM 'array' THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END IF;

    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_binding->'receiver_ids') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
        END IF;
        v_receiver_ids := pg_catalog.array_append(
            v_receiver_ids, (v_json_item #>> '{}')::uuid
        );
    END LOOP;
    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_binding->'locked_project_ids') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
        END IF;
        v_locked_project_ids := pg_catalog.array_append(
            v_locked_project_ids, (v_json_item #>> '{}')::uuid
        );
    END LOOP;

    FOR v_owner_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_owner_plan_item) IS DISTINCT FROM 'object'
           OR NOT aap_identity_offboarding_v1._has_exact_keys(
               v_owner_plan_item,
               ARRAY['project_id', 'expected_owner_id', 'receiver_id']
           )
           OR pg_catalog.jsonb_typeof(v_owner_plan_item->'project_id') IS DISTINCT FROM 'string'
           OR (v_owner_plan_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_owner_plan_item->'expected_owner_id') IS DISTINCT FROM 'string'
           OR (v_owner_plan_item->>'expected_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_owner_plan_item->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_owner_plan_item->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_project_id := (v_owner_plan_item->>'project_id')::uuid;
        v_receiver_id := (v_owner_plan_item->>'receiver_id')::uuid;
        IF (v_owner_plan_item->>'expected_owner_id')::uuid IS DISTINCT FROM v_target_user_id
           OR v_receiver_id = v_target_user_id
           OR NOT (v_project_id = ANY(v_scope_project_ids))
           OR NOT (v_project_id = ANY(v_locked_project_ids))
           OR NOT (v_receiver_id = ANY(v_receiver_ids)) THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
    END LOOP;

    FOR v_labeling_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_labeling_plan_item) IS DISTINCT FROM 'object'
           OR NOT aap_identity_offboarding_v1._has_exact_keys(
               v_labeling_plan_item,
               ARRAY['project_id', 'role', 'receiver_id', 'batch_ids', 'task_ids']
           )
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'project_id') IS DISTINCT FROM 'string'
           OR (v_labeling_plan_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'role') IS DISTINCT FROM 'string'
           OR v_labeling_plan_item->>'role' NOT IN ('annotator', 'reviewer')
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_labeling_plan_item->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'batch_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'task_ids') IS DISTINCT FROM 'array' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_project_id := (v_labeling_plan_item->>'project_id')::uuid;
        v_receiver_id := (v_labeling_plan_item->>'receiver_id')::uuid;
        IF NOT (v_project_id = ANY(v_scope_project_ids))
           OR NOT (v_project_id = ANY(v_locked_project_ids))
           OR v_receiver_id = v_target_user_id
           OR NOT (v_receiver_id = ANY(v_receiver_ids)) THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
    END LOOP;

    IF v_mode = 'emergency_suspend'
       AND (
           pg_catalog.jsonb_array_length(v_payload->'owner_plan') <> 0
           OR pg_catalog.jsonb_array_length(v_payload->'labeling_plan') <> 0
           OR pg_catalog.jsonb_array_length(v_transfers) <> 0
       ) THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;
    IF v_mode = 'handoff'
       AND pg_catalog.jsonb_array_length(v_payload->'unresolved') <> 0 THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_actor_reply := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, 'write_nowait'
    );
    IF v_actor_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor_reply #>> '{error,code}'
            WHEN 'user_inactive' THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'manager_required', 403,
                    pg_catalog.to_jsonb('当前账号已无管理权限'::text)
                );
            WHEN 'account_busy' THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            WHEN 'invalid_request' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_offboarding_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_offboarding_v1._failure('token_expired', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_busy', NULL, NULL);
            ELSE
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_actor_reply->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;

    SELECT account.role, account.is_active, account.credential_generation,
           account.disabled_kind
    INTO v_current_target_role, v_target_active, v_target_generation,
         v_target_disabled_kind
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    v_target_found := FOUND;
    IF NOT v_target_found
       OR v_target_generation IS DISTINCT FROM v_expected_generation
       OR v_target_active IS DISTINCT FROM v_expected_active
       OR v_current_target_role IS DISTINCT FROM v_scope_basis->>'target_role' THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END IF;
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;
    IF v_actor_id = v_target_user_id THEN
        RETURN aap_identity_offboarding_v1._failure(
            'self_offboard', 400,
            pg_catalog.to_jsonb('不能交接自己'::text)
        );
    END IF;
    IF v_target_active IS TRUE
       AND v_current_target_role = 'super_admin' THEN
        v_super_admin_status := aap_identity_v1.super_admin_status(
            v_target_user_id
        );
        IF pg_catalog.jsonb_typeof(v_super_admin_status) IS DISTINCT FROM 'object'
           OR v_super_admin_status->>'schema' IS DISTINCT FROM
              'aap-identity-super-admin-status/v1'
           OR pg_catalog.jsonb_typeof(v_super_admin_status->'other_unreserved') IS DISTINCT FROM 'number'
           OR (v_super_admin_status->>'other_unreserved') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding super-admin status';
        END IF;
        BEGIN
            v_other_unreserved :=
                (v_super_admin_status->>'other_unreserved')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding super-admin count';
        END;
        IF v_other_unreserved = 0 THEN
            RETURN aap_identity_offboarding_v1._failure(
                'last_super_admin', 400,
                pg_catalog.to_jsonb('不能停用最后一名超级管理员'::text)
            );
        END IF;
    END IF;
    IF v_actor_role = 'project_admin' THEN
        IF v_current_target_role IS DISTINCT FROM 'employee' THEN
            RETURN aap_identity_offboarding_v1._failure(
                'target_role_forbidden', 403,
                pg_catalog.to_jsonb(
                    '项目管理员仅能处理标注员/审核员账号'::text
                )
            );
        END IF;
        IF pg_catalog.cardinality(v_scope_project_ids) = 0
           AND v_target_active IS NOT TRUE THEN
            RETURN aap_identity_offboarding_v1._failure(
                'inactive_unmanaged', 403,
                pg_catalog.to_jsonb(
                    '该用户已停用且不在你管理的项目内，须由超级管理员处理'::text
                )
            );
        END IF;
        BEGIN
            SELECT scope.projects_exist, scope.all_projects_owned
            INTO STRICT v_scope_reply
            FROM aap_workspace_v1.identity_offboarding_replay_scope(
                v_actor_id, v_scope_project_ids
            ) AS scope;
        EXCEPTION WHEN no_data_found OR too_many_rows THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding scope reply';
        END;
        IF v_scope_reply.projects_exist IS DISTINCT FROM true
           OR v_scope_reply.all_projects_owned IS DISTINCT FROM true THEN
            RETURN aap_identity_offboarding_v1._failure(
                'scope_forbidden', 403,
                pg_catalog.to_jsonb('该用户存在不在你管理范围内的项目'::text)
            );
        END IF;
    END IF;

    IF NOT v_target_active
       AND v_target_disabled_kind IN ('deleted', 'historical_unknown') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'historical_target', 409,
            pg_catalog.to_jsonb('历史未知或已删除账号不可再次交接'::text)
        );
    END IF;
    IF NOT v_target_active
       AND v_target_disabled_kind NOT IN ('suspended', 'emergency_suspended') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'state_forbidden', 409,
            pg_catalog.to_jsonb('账号当前状态不可交接'::text)
        );
    END IF;

    v_owner_plan := v_payload->'owner_plan';
    v_labeling_plan := v_payload->'labeling_plan';
    v_transfers := v_payload->'transfers';
    v_unresolved := v_payload->'unresolved';
    SELECT pg_catalog.count(*)::bigint INTO v_expected_owner_count
    FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value);
    SELECT pg_catalog.count(*)::bigint INTO v_expected_label_count
    FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value);

    IF v_mode = 'handoff' THEN
        SELECT pg_catalog.count(*) FILTER (
                   WHERE item.value->>'role' = 'owner'
               )::bigint,
               pg_catalog.count(*) FILTER (
                   WHERE item.value->>'role' IN ('annotator', 'reviewer')
               )::bigint
        INTO v_actual_owner_count, v_actual_label_count
        FROM pg_catalog.jsonb_array_elements(v_transfers) AS item(value);
        IF v_expected_owner_count <> v_actual_owner_count
           OR v_expected_label_count <> v_actual_label_count THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;

        FOR v_owner_plan_item IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value)
        LOOP
            SELECT pg_catalog.count(*)::bigint INTO v_match_count
            FROM pg_catalog.jsonb_array_elements(v_transfers) AS item(value)
            WHERE item.value->>'role' = 'owner'
              AND item.value->>'project_id' = v_owner_plan_item->>'project_id'
              AND item.value->>'receiver_id' = v_owner_plan_item->>'receiver_id'
              AND item.value->'batch_ids' = '[]'::jsonb
              AND item.value->>'task_count' = '0'
              AND item.value->>'lock_count' = '0';
            IF v_match_count <> 1 THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
        END LOOP;
        FOR v_labeling_plan_item IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value)
        LOOP
            SELECT pg_catalog.count(*)::bigint INTO v_match_count
            FROM pg_catalog.jsonb_array_elements(v_transfers) AS item(value)
            WHERE item.value->>'role' = v_labeling_plan_item->>'role'
              AND item.value->>'project_id' = v_labeling_plan_item->>'project_id'
              AND item.value->>'receiver_id' = v_labeling_plan_item->>'receiver_id'
              AND item.value->'batch_ids' = v_labeling_plan_item->'batch_ids'
              AND item.value->>'task_count' =
                  pg_catalog.jsonb_array_length(v_labeling_plan_item->'task_ids')::text;
            IF v_match_count <> 1 THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
        END LOOP;
    END IF;

    v_owner_result := pg_catalog.jsonb_build_object(
        'changed_project_ids', '[]'::jsonb,
        'changed_count', 0
    );
    IF pg_catalog.jsonb_array_length(v_owner_plan) > 0 THEN
        v_owner_result := aap_workspace_v1.transfer_identity_offboarding_owners(
            v_target_user_id, v_owner_plan
        );
    END IF;
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        v_owner_result, ARRAY['changed_project_ids', 'changed_count']
    )
       OR pg_catalog.jsonb_typeof(v_owner_result->'changed_project_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_owner_result->'changed_count') IS DISTINCT FROM 'number'
       OR (v_owner_result->>'changed_count') !~ '^(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding Workspace transfer reply';
    END IF;
    v_previous_id := NULL;
    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(
            v_owner_result->'changed_project_ids'
        ) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding Workspace project IDs';
        END IF;
        v_actual_owner_ids := pg_catalog.array_append(
            v_actual_owner_ids, (v_json_item #>> '{}')::uuid
        );
    END LOOP;
    FOR v_owner_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value)
    LOOP
        v_owner_project_ids := pg_catalog.array_append(
            v_owner_project_ids, (v_owner_plan_item->>'project_id')::uuid
        );
    END LOOP;
    IF v_actual_owner_ids IS DISTINCT FROM v_owner_project_ids
       OR (v_owner_result->>'changed_count')::bigint IS DISTINCT FROM
          pg_catalog.cardinality(v_owner_project_ids)::bigint THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Identity offboarding Workspace transfer result mismatch';
    END IF;

    v_labeling_result := '[]'::jsonb;
    IF pg_catalog.jsonb_array_length(v_labeling_plan) > 0 THEN
        v_labeling_result := aap_labeling_v1.transfer_identity_offboarding_work(
            v_target_user_id, v_labeling_plan
        );
    END IF;
    IF pg_catalog.jsonb_typeof(v_labeling_result) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_labeling_result) <>
          pg_catalog.jsonb_array_length(v_labeling_plan) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding Labeling transfer reply';
    END IF;
    v_index := 0;
    FOR v_labeling_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value)
    LOOP
        v_label_result_item := v_labeling_result->v_index;
        IF NOT aap_identity_offboarding_v1._has_exact_keys(
            v_label_result_item,
            ARRAY['project_id', 'role', 'batch_ids', 'task_ids', 'task_count']
        )
           OR v_label_result_item->>'project_id' IS DISTINCT FROM
              v_labeling_plan_item->>'project_id'
           OR v_label_result_item->>'role' IS DISTINCT FROM
              v_labeling_plan_item->>'role'
           OR v_label_result_item->'batch_ids' IS DISTINCT FROM
              v_labeling_plan_item->'batch_ids'
           OR v_label_result_item->'task_ids' IS DISTINCT FROM
              v_labeling_plan_item->'task_ids'
           OR pg_catalog.jsonb_typeof(v_label_result_item->'task_count') IS DISTINCT FROM 'number'
           OR (v_label_result_item->>'task_count') !~ '^(0|[1-9][0-9]*)$'
           OR (v_label_result_item->>'task_count')::bigint IS DISTINCT FROM
              pg_catalog.jsonb_array_length(v_labeling_plan_item->'task_ids')::bigint THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Identity offboarding Labeling transfer result mismatch';
        END IF;
        v_index := v_index + 1;
    END LOOP;

    UPDATE public.users AS account
    SET is_active = false,
        status = 'offline',
        disabled_kind = CASE
            WHEN v_mode = 'handoff' THEN 'suspended'
            ELSE 'emergency_suspended'
        END,
        disabled_at = pg_catalog.clock_timestamp(),
        disabled_by = v_actor_id,
        disabled_reason = v_normalized_reason
    WHERE account.id = v_target_user_id
      AND account.credential_generation = v_expected_generation
      AND account.is_active = v_expected_active;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Identity offboarding target update did not affect one row';
    END IF;

    v_retirement := aap_identity_v1.retire_account_credentials(v_target_user_id);
    IF pg_catalog.jsonb_typeof(v_retirement) IS DISTINCT FROM 'object'
       OR v_retirement->>'schema' IS DISTINCT FROM 'aap-identity-retirement/v1'
       OR pg_catalog.jsonb_typeof(v_retirement->'generation') IS DISTINCT FROM 'number'
       OR (v_retirement->>'generation') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(v_retirement->'revoked_key_ids') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding credential retirement reply';
    END IF;
    BEGIN
        v_new_generation := (v_retirement->>'generation')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding credential generation';
    END;
    IF v_new_generation IS DISTINCT FROM v_expected_generation + 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Identity offboarding credential generation mismatch';
    END IF;
    v_revoked_key_ids := v_retirement->'revoked_key_ids';
    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_revoked_key_ids) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding revoked key IDs';
        END IF;
    END LOOP;

    v_released_lock_count := aap_labeling_v1.release_identity_offboarding_task_locks(
        v_target_user_id
    );
    IF v_released_lock_count IS NULL OR v_released_lock_count < 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding task-lock release reply';
    END IF;

    v_context_actor := pg_catalog.jsonb_build_object(
        'actor_id', v_principal->'actor_id',
        'email', v_principal->'email',
        'role', v_principal->'role'
    );
    v_audit_id := aap_audit_v1.record_identity_offboarding_event(
        v_context_actor,
        v_target_user_id,
        pg_catalog.jsonb_build_object(
            'mode', v_mode,
            'reason_present', v_reason_present,
            'transfers', v_transfers,
            'unresolved', v_unresolved,
            'revoked_api_key_count', pg_catalog.jsonb_array_length(v_revoked_key_ids)
        ),
        v_context
    );
    IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding Audit ID';
    END IF;

    v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_user_record IS NULL
       OR pg_catalog.jsonb_typeof(v_user_record) IS DISTINCT FROM 'object'
       OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
       OR v_user_record->'is_active' IS DISTINCT FROM 'false'::jsonb
       OR v_user_record->>'status' IS DISTINCT FROM 'offline'
       OR v_user_record->>'disabled_kind' IS DISTINCT FROM
          (CASE WHEN v_mode = 'handoff' THEN 'suspended' ELSE 'emergency_suspended' END)
       OR v_user_record->'disabled_reason' IS DISTINCT FROM
          pg_catalog.to_jsonb(v_normalized_reason) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding account reply';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'user', v_user_record,
        'status', 'suspended',
        'mode', v_mode,
        'transfers', v_transfers,
        'unresolved', v_unresolved,
        'revoked_api_key_ids', v_revoked_key_ids,
        'audit_id', v_audit_id
    );

    INSERT INTO public.identity_offboarding_operation_receipts (
        operation_id, actor_id, operation, target_user_id,
        request_fingerprint, derivation_id, target_generation,
        target_is_active, result, scope_basis, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, 'commit_offboarding', v_target_user_id,
        v_request_fingerprint, v_derivation_id, v_new_generation,
        false, v_result, v_scope_basis, v_audit_id
    );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-offboarding-native/v1',
        'ok', true,
        'operation', 'commit_offboarding',
        'operation_id', v_operation_id,
        'principal', v_principal,
        'phase', 'apply',
        'data', pg_catalog.jsonb_build_object('result', v_result),
        'replayed', false
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_offboarding_v1.apply_offboarding_command(
    jsonb
) FROM PUBLIC;
