-- Native Identity administrator account deletion.
CREATE FUNCTION aap_identity_deletions_v1._failure(
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
        'schema', 'aap-identity-deletions-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code,
            'status_code', p_status_code,
            'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deletions_v1._failure(
    text, integer, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deletions_v1._has_exact_keys(
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
REVOKE ALL ON FUNCTION aap_identity_deletions_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deletions_v1.deletion_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deletions-policy/v1',
        'policy', 'aap-identity-deletions/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deletions_v1.deletion_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deletions_v1.execute_deletion_command(
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
    v_transfer_to_user_id uuid;
    v_locked_user_id uuid;
    v_target_found boolean := false;
    v_target_email text;
    v_target_role text;
    v_target_active boolean;
    v_target_generation bigint;
    v_actor_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_scope_has_projects boolean;
    v_scope_all_projects_owned boolean;
    v_super_admin_status jsonb;
    v_other_unreserved bigint;
    v_pending_task_count bigint;
    v_locked_task_count bigint;
    v_sample_task_ids uuid[];
    v_pending_project_ids uuid[];
    v_receiver_found boolean := false;
    v_receiver_active boolean;
    v_receiver_role text;
    v_receiver_covered boolean;
    v_transferred_count bigint := 0;
    v_audit_id bigint;
    v_user_record jsonb;
    v_result jsonb;
    v_receipt record;
    v_receipt_found boolean := false;
    v_project_busy_detail jsonb := pg_catalog.jsonb_build_object(
        'reason', 'project_busy'
    );
BEGIN
    IF NOT aap_identity_deletions_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation', 'operation_id', 'actor',
            'expected_key_hash', 'request_fingerprint', 'derivation_id',
            'audit_context', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-deletions-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR p_command->>'operation' IS DISTINCT FROM 'delete_account'
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
       OR NOT aap_identity_deletions_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active',
               'credential_type', 'credential_id', 'generation', 'scopes',
               'expires_at'
           ]
       ) OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}')
            IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_identity_deletions_v1._has_exact_keys(
           p_command->'audit_context',
           ARRAY['method', 'path', 'ip', 'request_id']
       ) OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_deletions_v1._failure('invalid_request', 400, NULL);
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

    IF NOT aap_identity_deletions_v1._has_exact_keys(
        v_payload, ARRAY['target_user_id', 'transfer_to_user_id']
    ) OR pg_catalog.jsonb_typeof(v_payload->'target_user_id')
             IS DISTINCT FROM 'string'
      OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR COALESCE(
          pg_catalog.jsonb_typeof(v_payload->'transfer_to_user_id')
              NOT IN ('string', 'null'),
          true
      )
      OR (pg_catalog.jsonb_typeof(v_payload->'transfer_to_user_id') = 'string'
          AND (v_payload->>'transfer_to_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN aap_identity_deletions_v1._failure('invalid_request', 400, NULL);
    END IF;
    v_target_user_id := (v_payload->>'target_user_id')::uuid;
    IF pg_catalog.jsonb_typeof(v_payload->'transfer_to_user_id') = 'string' THEN
        v_transfer_to_user_id := (v_payload->>'transfer_to_user_id')::uuid;
    END IF;

    IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    BEGIN
        FOR v_locked_user_id IN
            SELECT account.id
            FROM public.users AS account
            WHERE account.id = ANY(
                ARRAY[v_actor_id, v_target_user_id, v_transfer_to_user_id]::uuid[]
            )
            ORDER BY account.id
            FOR UPDATE OF account NOWAIT
        LOOP
            NULL;
        END LOOP;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END;

    SELECT account.email, account.role, account.is_active,
           account.credential_generation
    INTO v_target_email, v_target_role, v_target_active,
         v_target_generation
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    v_target_found := FOUND;

    v_actor_reply := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, 'write_nowait'
    );
    IF v_actor_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor_reply #>> '{error,code}'
            WHEN 'invalid_request' THEN
                RETURN aap_identity_deletions_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_deletions_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_deletions_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_deletions_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_deletions_v1._failure('token_expired', NULL, NULL);
            WHEN 'user_inactive' THEN
                RETURN aap_identity_deletions_v1._failure('user_inactive', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_deletions_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_deletions_v1._failure('credential_busy', NULL, NULL);
            WHEN 'account_busy' THEN
                RETURN aap_identity_deletions_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            ELSE
                RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_actor_reply->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_deletions_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;
    IF NOT v_target_found THEN
        RETURN aap_identity_deletions_v1._failure(
            'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-deletions-operation/v1:' || v_operation_id::text,
            0
        )
    ) THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    SELECT receipt.actor_id, receipt.operation, receipt.target_user_id,
           receipt.transfer_to_user_id, receipt.request_fingerprint,
           receipt.derivation_id, receipt.target_generation,
           receipt.target_is_active, receipt.result
    INTO v_receipt
    FROM public.identity_deletion_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
           OR v_receipt.transfer_to_user_id IS DISTINCT FROM v_transfer_to_user_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_deletions_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        IF v_receipt.target_generation IS DISTINCT FROM v_target_generation
           OR v_receipt.target_is_active IS DISTINCT FROM v_target_active THEN
            RETURN aap_identity_deletions_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-deletions-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_receipt.result,
            'replayed', true
        );
    END IF;

    IF v_actor_id = v_target_user_id THEN
        RETURN aap_identity_deletions_v1._failure(
            'self_delete', 400,
            pg_catalog.to_jsonb('不能删除自己'::text)
        );
    END IF;

    -- Legacy DELETE deliberately accepts an inactive target before checking
    -- target role/scope, recipient state, task work or Audit/metadata effects.
    IF NOT v_target_active THEN
        v_user_record := aap_identity_directory_v1._directory_user(
            v_target_user_id
        );
        IF v_user_record IS NULL
           OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
           OR v_user_record->'is_active' IS DISTINCT FROM 'false'::jsonb THEN
            RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END IF;
        v_result := pg_catalog.jsonb_build_object('user', v_user_record);
        INSERT INTO public.identity_deletion_operation_receipts (
            operation_id, actor_id, operation, target_user_id,
            transfer_to_user_id, request_fingerprint, derivation_id,
            target_generation, target_is_active, result, audit_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_target_user_id,
            v_transfer_to_user_id, v_request_fingerprint, v_derivation_id,
            v_target_generation, false, v_result, NULL
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-deletions-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_result,
            'replayed', false
        );
    END IF;

    BEGIN
        SELECT scope.has_projects, scope.all_projects_owned
        INTO STRICT v_scope_has_projects, v_scope_all_projects_owned
        FROM aap_workspace_v1.lock_identity_deletion_scope(
            v_actor_id, v_target_user_id
        ) AS scope;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deletions_v1._failure(
            'project_busy', 409, v_project_busy_detail
        );
    WHEN no_data_found OR too_many_rows THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END;
    IF v_scope_has_projects IS NULL OR v_scope_all_projects_owned IS NULL THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END IF;

    IF v_actor_role = 'project_admin' THEN
        IF v_target_role IS DISTINCT FROM 'employee' THEN
            RETURN aap_identity_deletions_v1._failure(
                'delete_role_forbidden', 403,
                pg_catalog.to_jsonb('项目管理员仅能删除员工账号'::text)
            );
        END IF;
        IF NOT v_scope_all_projects_owned THEN
            RETURN aap_identity_deletions_v1._failure(
                'delete_scope_forbidden', 403,
                pg_catalog.to_jsonb('该用户跨多个项目，须由超级管理员处理'::text)
            );
        END IF;
    END IF;

    IF v_target_role = 'super_admin' THEN
        v_super_admin_status := aap_identity_v1.super_admin_status(
            v_target_user_id
        );
        IF pg_catalog.jsonb_typeof(v_super_admin_status) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_super_admin_status->'other_unreserved')
                IS DISTINCT FROM 'number'
           OR (v_super_admin_status->>'other_unreserved') !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END IF;
        v_other_unreserved := (v_super_admin_status->>'other_unreserved')::bigint;
        IF v_other_unreserved = 0 THEN
            RETURN aap_identity_deletions_v1._failure(
                'last_super_admin', 400,
                pg_catalog.to_jsonb('不能删除最后一名超级管理员'::text)
            );
        END IF;
    END IF;

    BEGIN
        SELECT work.pending_task_count, work.locked_task_count,
               work.sample_task_ids, work.pending_project_ids
        INTO STRICT v_pending_task_count, v_locked_task_count,
                    v_sample_task_ids, v_pending_project_ids
        FROM aap_labeling_v1.identity_deletion_work(v_target_user_id) AS work;
    EXCEPTION WHEN no_data_found OR too_many_rows THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END;
    IF v_pending_task_count IS NULL OR v_pending_task_count < 0
       OR v_locked_task_count IS NULL OR v_locked_task_count < 0
       OR v_sample_task_ids IS NULL
       OR pg_catalog.cardinality(v_sample_task_ids) > 5
       OR pg_catalog.array_position(v_sample_task_ids, NULL::uuid) IS NOT NULL
       OR v_pending_project_ids IS NULL
       OR pg_catalog.array_position(v_pending_project_ids, NULL::uuid) IS NOT NULL THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END IF;

    IF v_transfer_to_user_id IS NULL
       AND (v_pending_task_count > 0 OR v_locked_task_count > 0) THEN
        RETURN aap_identity_deletions_v1._failure(
            'has_pending_tasks', 409,
            pg_catalog.jsonb_build_object(
                'reason', 'has_pending_tasks',
                'pending_task_count', v_pending_task_count,
                'locked_task_count', v_locked_task_count,
                'sample_task_ids', pg_catalog.to_jsonb(v_sample_task_ids),
                'message', pg_catalog.format(
                    '该用户当前有 %s 个未完成任务、%s 个锁定任务，需先转交',
                    v_pending_task_count, v_locked_task_count
                )
            )
        );
    END IF;

    IF v_transfer_to_user_id IS NOT NULL THEN
        SELECT account.is_active, account.role
        INTO v_receiver_active, v_receiver_role
        FROM public.users AS account
        WHERE account.id = v_transfer_to_user_id;
        v_receiver_found := FOUND;
        IF NOT v_receiver_found OR NOT v_receiver_active THEN
            RETURN aap_identity_deletions_v1._failure(
                'receiver_unavailable', 400,
                pg_catalog.to_jsonb('转交目标用户不存在或已禁用'::text)
            );
        END IF;
        IF v_transfer_to_user_id = v_target_user_id THEN
            RETURN aap_identity_deletions_v1._failure(
                'receiver_is_target', 400,
                pg_catalog.to_jsonb('转交目标不能与被删用户相同'::text)
            );
        END IF;
        IF v_receiver_role NOT IN ('employee', 'project_admin') THEN
            RETURN aap_identity_deletions_v1._failure(
                'receiver_role_invalid', 400,
                pg_catalog.to_jsonb('转交目标角色不合法'::text)
            );
        END IF;

        IF v_actor_role = 'project_admin'
           AND v_transfer_to_user_id <> v_actor_id THEN
            BEGIN
                SELECT aap_workspace_v1.identity_deletion_transfer_covered(
                    v_transfer_to_user_id, v_pending_project_ids
                )
                INTO STRICT v_receiver_covered;
            EXCEPTION WHEN no_data_found OR too_many_rows THEN
                RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
            END;
            IF v_receiver_covered IS NULL THEN
                RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
            END IF;
            IF NOT v_receiver_covered THEN
                RETURN aap_identity_deletions_v1._failure(
                    'receiver_scope_forbidden', 403,
                    pg_catalog.to_jsonb('转交目标必须是待转交任务所在项目的成员'::text)
                );
            END IF;
        END IF;

        BEGIN
            v_transferred_count := aap_labeling_v1.transfer_identity_deletion_work(
                v_target_user_id, v_transfer_to_user_id
            );
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_deletions_v1._failure(
                'account_busy', 409,
                pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
            );
        END;
        IF v_transferred_count IS NULL OR v_transferred_count < 0 THEN
            RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END IF;
    END IF;

    UPDATE public.users AS account
    SET is_active = false,
        status = 'offline',
        disabled_kind = 'deleted',
        disabled_at = pg_catalog.clock_timestamp(),
        disabled_by = v_actor_id,
        disabled_reason = '管理员删除账号'
    WHERE account.id = v_target_user_id;

    v_audit_id := aap_audit_v1.record_identity_deletion_event(
        pg_catalog.jsonb_build_object(
            'actor_id', v_principal->'actor_id',
            'email', v_principal->'email',
            'role', v_principal->'role'
        ),
        v_target_user_id,
        pg_catalog.jsonb_build_object(
            'email', v_target_email,
            'role', v_target_role,
            'transferred_to', v_transfer_to_user_id,
            'transferred_count', v_transferred_count,
            'released_locks', CASE WHEN v_transfer_to_user_id IS NOT NULL
                                   THEN v_locked_task_count ELSE 0::bigint END
        ),
        v_context
    );
    IF v_audit_id IS NULL THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END IF;

    v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_user_record IS NULL
       OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
       OR v_user_record->'is_active' IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid frozen Identity deletion user result';
    END IF;
    v_result := pg_catalog.jsonb_build_object('user', v_user_record);
    INSERT INTO public.identity_deletion_operation_receipts (
        operation_id, actor_id, operation, target_user_id,
        transfer_to_user_id, request_fingerprint, derivation_id,
        target_generation, target_is_active, result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_operation, v_target_user_id,
        v_transfer_to_user_id, v_request_fingerprint, v_derivation_id,
        v_target_generation, false, v_result, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deletions-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_result,
        'replayed', false
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    WHEN OTHERS THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deletions_v1.execute_deletion_command(
    jsonb
) FROM PUBLIC;
