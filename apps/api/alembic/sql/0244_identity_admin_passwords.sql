-- Native transactional administrator password reset.
CREATE FUNCTION aap_identity_admin_passwords_v1._failure(
    p_code text,
    p_status_code integer DEFAULT NULL,
    p_detail text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-admin-passwords-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code, 'status_code', p_status_code, 'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_admin_passwords_v1._failure(
    text, integer, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_admin_passwords_v1._has_exact_keys(
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
REVOKE ALL ON FUNCTION aap_identity_admin_passwords_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_admin_passwords_v1.admin_password_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-admin-passwords-policy/v1',
        'policy', 'aap-identity-admin-passwords/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_admin_passwords_v1.admin_password_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_admin_passwords_v1.execute_admin_password_command(
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
    v_actor jsonb;
    v_payload jsonb;
    v_context jsonb;
    v_expected_key_hash text;
    v_new_password_hash text;
    v_operation_id uuid;
    v_target_user_id uuid;
    v_actor_id uuid;
    v_actor_uuid text;
    v_operation text;
    v_fingerprint text;
    v_derivation_id text;
    v_actor_fence jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_locked_user_id uuid;
    v_target_email text;
    v_target_role text;
    v_target_active boolean;
    v_target_password_hash text;
    v_target_reset_at timestamptz;
    v_actor_rank integer;
    v_target_rank integer;
    v_in_scope boolean;
    v_replayed boolean := false;
    v_receipt record;
    v_reset_at timestamptz;
    v_password_state_fingerprint text;
    v_audit_id bigint;
BEGIN
    IF NOT aap_identity_admin_passwords_v1._has_exact_keys(
        p_command, ARRAY[
            'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'payload',
            'new_password_hash'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-admin-passwords-native/v1'
       OR p_command->>'operation' IS DISTINCT FROM 'admin_reset_password'
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'new_password_hash') IS DISTINCT FROM 'string'
       OR (p_command->>'new_password_hash') COLLATE "C"
            !~ '^\$2[aby]\$12\$[./A-Za-z0-9]{53}$'
       OR NOT aap_identity_admin_passwords_v1._has_exact_keys(
           p_command->'audit_context', ARRAY['method', 'path', 'ip', 'request_id']
       ) OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context')
       OR NOT aap_identity_admin_passwords_v1._has_exact_keys(
           p_command->'payload', ARRAY['target_user_id']
       ) OR pg_catalog.jsonb_typeof(p_command #> '{payload,target_user_id}')
            IS DISTINCT FROM 'string'
       OR (p_command #>> '{payload,target_user_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_admin_passwords_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_actor := p_command->'actor';
    IF NOT aap_identity_admin_passwords_v1._has_exact_keys(
        v_actor, ARRAY[
            'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
            'credential_id', 'generation', 'scopes', 'expires_at'
        ]
    ) OR pg_catalog.jsonb_typeof(v_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (v_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_admin_passwords_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor_uuid := (v_actor->>'actor_id');
    v_actor_id := v_actor_uuid::uuid;
    v_payload := p_command->'payload';
    v_target_user_id := (v_payload->>'target_user_id')::uuid;
    v_operation := p_command->>'operation';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_new_password_hash := p_command->>'new_password_hash';

    -- User credential/role writers and this reset use the same deterministic
    -- actor/target ordering. The Account fence below is intentionally reused
    -- only after both User rows are already held.
    BEGIN
        FOR v_locked_user_id IN
            SELECT account.id
            FROM public.users AS account
            WHERE account.id = ANY(ARRAY[v_actor_id, v_target_user_id]::uuid[])
            ORDER BY account.id
            FOR UPDATE OF account NOWAIT
        LOOP
            NULL;
        END LOOP;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'account_busy', 409, '账号正在更新，请刷新后重试'
        );
    END;

    v_actor_fence := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, 'write_nowait'
    );
    IF v_actor_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor_fence #>> '{error,code}'
            WHEN 'invalid_request' THEN
                RETURN aap_identity_admin_passwords_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_admin_passwords_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_admin_passwords_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_admin_passwords_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_admin_passwords_v1._failure('token_expired', NULL, NULL);
            WHEN 'user_inactive' THEN
                RETURN aap_identity_admin_passwords_v1._failure('user_inactive', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_admin_passwords_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_admin_passwords_v1._failure('credential_busy', NULL, NULL);
            WHEN 'account_busy' THEN
                RETURN aap_identity_admin_passwords_v1._failure(
                    'account_busy', 409, '账号正在更新，请刷新后重试'
                );
            ELSE
                RETURN aap_identity_admin_passwords_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_actor_fence->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'manager_required', 403, '当前账号已无管理权限'
        );
    END IF;

    SELECT account.email, account.role, account.is_active,
           account.password_hash, account.password_admin_reset_at
    INTO v_target_email, v_target_role, v_target_active,
         v_target_password_hash, v_target_reset_at
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    IF NOT FOUND OR NOT v_target_active THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'target_not_found', 404, '用户不存在'
        );
    END IF;
    IF v_target_user_id = v_actor_id THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'self_reset', 400, '不能给自己重置密码（请用「修改密码」）'
        );
    END IF;

    v_actor_rank := CASE v_actor_role
        WHEN 'super_admin' THEN 0
        WHEN 'project_admin' THEN 1
        ELSE 99
    END;
    v_target_rank := CASE v_target_role
        WHEN 'super_admin' THEN 0
        WHEN 'project_admin' THEN 1
        WHEN 'reviewer' THEN 2
        WHEN 'employee' THEN 3
        WHEN 'annotator' THEN 3
        WHEN 'viewer' THEN 4
        ELSE 99
    END;
    IF v_target_rank <= v_actor_rank THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'role_forbidden', 403, '只能重置等级低于你的用户的密码'
        );
    END IF;

    IF v_actor_role = 'project_admin' AND v_target_role <> 'employee' THEN
        BEGIN
            v_in_scope := aap_workspace_v1.admin_password_target_in_owned_project(
                v_actor_id, v_target_user_id
            );
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_admin_passwords_v1._failure(
                'account_busy', 409, '账号正在更新，请刷新后重试'
            );
        END;
        IF NOT v_in_scope THEN
            RETURN aap_identity_admin_passwords_v1._failure(
                'target_out_of_scope', 403, '该用户不在你管理的项目内'
            );
        END IF;
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-admin-passwords-operation/v1:' || v_operation_id::text,
            0
        )
    ) THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'account_busy', 409, '账号正在更新，请刷新后重试'
        );
    END IF;

    SELECT receipt.operation, receipt.actor_id, receipt.target_user_id,
           receipt.request_fingerprint, receipt.derivation_id,
           receipt.target_email, receipt.password_admin_reset_at,
           receipt.password_state_fingerprint, receipt.audit_id
    INTO v_receipt
    FROM public.identity_admin_password_reset_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    IF FOUND THEN
        IF v_receipt.operation IS DISTINCT FROM 'admin_reset_password'
           OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_admin_passwords_v1._failure('operation_conflict', 409, NULL);
        END IF;
        v_password_state_fingerprint := pg_catalog.encode(
            public.digest(
                pg_catalog.convert_to(
                    'aap-identity-admin-password-state/v1:'
                    || v_operation_id::text || ':' || v_target_password_hash,
                    'UTF8'
                ),
                'sha256'
            ),
            'hex'
        );
        IF v_target_reset_at IS DISTINCT FROM v_receipt.password_admin_reset_at
           OR v_password_state_fingerprint IS DISTINCT FROM
               v_receipt.password_state_fingerprint THEN
            RETURN aap_identity_admin_passwords_v1._failure('operation_conflict', 409, NULL);
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-admin-passwords-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', pg_catalog.jsonb_build_object(
                'target_user_id', v_target_user_id,
                'target_email', v_receipt.target_email,
                'password_admin_reset_at', v_receipt.password_admin_reset_at
            ),
            'replayed', true
        );
    END IF;

    v_reset_at := pg_catalog.clock_timestamp();
    UPDATE public.users
    SET password_hash = v_new_password_hash,
        password_admin_reset_at = v_reset_at
    WHERE id = v_target_user_id;
    v_password_state_fingerprint := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                'aap-identity-admin-password-state/v1:'
                || v_operation_id::text || ':' || v_new_password_hash,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    v_audit_id := aap_audit_v1.record_identity_admin_password_reset(
        pg_catalog.jsonb_build_object(
            'actor_id', v_principal->'actor_id',
            'email', v_principal->'email',
            'role', v_principal->'role'
        ),
        v_target_user_id,
        v_context,
        pg_catalog.jsonb_build_object(
            'target_email', v_target_email,
            'target_role', v_target_role
        )
    );
    INSERT INTO public.identity_admin_password_reset_receipts (
        operation_id, actor_id, target_user_id, operation,
        request_fingerprint, derivation_id, target_email,
        password_admin_reset_at, password_state_fingerprint, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_target_user_id, 'admin_reset_password',
        v_fingerprint, v_derivation_id, v_target_email,
        v_reset_at, v_password_state_fingerprint, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-admin-passwords-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', pg_catalog.jsonb_build_object(
            'target_user_id', v_target_user_id,
            'target_email', v_target_email,
            'password_admin_reset_at', v_reset_at
        ),
        'replayed', false
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_admin_passwords_v1._failure(
            'account_busy', 409, '账号正在更新，请刷新后重试'
        );
    WHEN OTHERS THEN
        RETURN aap_identity_admin_passwords_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)
    FROM PUBLIC;
