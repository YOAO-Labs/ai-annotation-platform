-- Native Identity preview and mutation of platform account roles.
CREATE FUNCTION aap_identity_platform_roles_v1._failure(
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
        'schema', 'aap-identity-platform-roles-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code,
            'status_code', p_status_code,
            'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_platform_roles_v1._failure(
    text, integer, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_platform_roles_v1._has_exact_keys(
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
REVOKE ALL ON FUNCTION aap_identity_platform_roles_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_platform_roles_v1.platform_role_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-platform-roles-policy/v1',
        'policy', 'aap-identity-platform-roles/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_platform_roles_v1.platform_role_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_platform_roles_v1.execute_platform_role_command(
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
    v_payload jsonb;
    v_context jsonb;
    v_expected_key_hash text;
    v_request_fingerprint text;
    v_derivation_id text;
    v_actor_id uuid;
    v_target_user_id uuid;
    v_requested_role text;
    v_principal_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_locked_user_id uuid;
    v_target_email text;
    v_target_role text;
    v_target_active boolean;
    v_receipt record;
    v_receipt_found boolean := false;
    v_user_record jsonb;
    v_result jsonb;
    v_audit_id bigint;
    v_snapshot record;
    v_workspace_facts jsonb;
    v_labeling_facts jsonb;
    v_blockers text[] := ARRAY[]::text[];
    v_project_items jsonb;
    v_assigned_batch_count bigint := 0;
    v_assigned_task_count bigint := 0;
    v_review_task_count bigint := 0;
    v_non_viewer_membership_count bigint := 0;
    v_owned_project_count bigint := 0;
    v_unfinished_count bigint := 0;
    v_super_admin_status jsonb;
    v_project_lock_acquired boolean;
BEGIN
    IF NOT aap_identity_platform_roles_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-platform-roles-native/v1' THEN
        RETURN aap_identity_platform_roles_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation := p_command->>'operation';
    IF pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR v_operation NOT IN ('preview_role_change', 'change_platform_role')
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR NOT aap_identity_platform_roles_v1._has_exact_keys(
           p_command->'audit_context', ARRAY['method', 'path', 'ip', 'request_id']
       ) OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context')
       OR NOT aap_identity_platform_roles_v1._has_exact_keys(
           p_command->'payload', ARRAY['target_user_id', 'role']
       ) OR pg_catalog.jsonb_typeof(p_command #> '{payload,target_user_id}')
            IS DISTINCT FROM 'string'
       OR (p_command #>> '{payload,target_user_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_command #> '{payload,role}') IS DISTINCT FROM 'string'
       OR NOT aap_identity_platform_roles_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
               'credential_id', 'generation', 'scopes', 'expires_at'
           ]
       ) OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}')
            IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_platform_roles_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_payload := p_command->'payload';
    v_target_user_id := (v_payload->>'target_user_id')::uuid;
    v_requested_role := v_payload->>'role';
    v_context := p_command->'audit_context';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_request_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';

    IF v_operation = 'change_platform_role' THEN
        IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
            RETURN aap_identity_platform_roles_v1._failure(
                'account_busy', 409, to_jsonb('账号正在更新，请刷新后重试'::text)
            );
        END IF;

        BEGIN
            FOR v_locked_user_id IN
                SELECT account.id
                FROM public.users AS account
                WHERE account.id = ANY(
                    ARRAY[v_actor_id, v_target_user_id]::uuid[]
                )
                ORDER BY account.id
                FOR UPDATE OF account NOWAIT
            LOOP
                NULL;
            END LOOP;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_platform_roles_v1._failure(
                'account_busy', 409, to_jsonb('账号正在更新，请刷新后重试'::text)
            );
        END;
    END IF;

    v_principal_reply := aap_identity_accounts_v1._lock_current_actor(
        v_actor,
        v_expected_key_hash,
        CASE WHEN v_operation = 'preview_role_change' THEN 'read'
             ELSE 'write_nowait' END
    );
    IF v_principal_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_principal_reply #>> '{error,code}'
            WHEN 'invalid_request' THEN
                RETURN aap_identity_platform_roles_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_platform_roles_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_platform_roles_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_platform_roles_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_platform_roles_v1._failure('token_expired', NULL, NULL);
            WHEN 'user_inactive' THEN
                RETURN aap_identity_platform_roles_v1._failure('user_inactive', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_platform_roles_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_platform_roles_v1._failure('credential_busy', NULL, NULL);
            WHEN 'account_busy' THEN
                RETURN aap_identity_platform_roles_v1._failure(
                    'account_busy', 409,
                    to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            ELSE
                RETURN aap_identity_platform_roles_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_principal_reply->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role IS DISTINCT FROM 'super_admin' THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'super_admin_required', 403, to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;

    IF v_requested_role NOT IN (
        'super_admin', 'project_admin', 'employee', 'viewer'
    ) THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'invalid_platform_role', 400,
            to_jsonb('非法平台角色: ' || v_requested_role)
        );
    END IF;

    IF v_operation = 'preview_role_change' THEN
        SELECT * INTO v_snapshot
        FROM aap_identity_platform_roles_v1._preview_snapshot(v_target_user_id);
        IF NOT FOUND THEN
            RETURN aap_identity_platform_roles_v1._failure(
                'target_not_found', 404, to_jsonb('用户不存在'::text)
            );
        END IF;
        v_user_record := v_snapshot.user_record;
        v_workspace_facts := v_snapshot.workspace_facts;
        v_labeling_facts := v_snapshot.labeling_facts;
        v_target_role := v_user_record->>'role';

        IF v_target_user_id = v_actor_id THEN
            v_blockers := pg_catalog.array_append(v_blockers, '不能修改自己的角色');
        END IF;
        IF v_requested_role = v_target_role THEN
            v_blockers := pg_catalog.array_append(v_blockers, '目标角色与当前角色相同');
        END IF;
        IF v_target_role = 'super_admin'
           AND v_requested_role <> 'super_admin'
           AND v_snapshot.other_unreserved_super_admins = 0 THEN
            v_blockers := pg_catalog.array_append(
                v_blockers, '不能降级最后一名超级管理员'
            );
        END IF;

        IF v_requested_role <> v_target_role THEN
            v_non_viewer_membership_count :=
                (v_workspace_facts->>'non_viewer_membership_count')::bigint;
            v_owned_project_count :=
                (v_workspace_facts->>'owned_project_count')::bigint;
            v_unfinished_count :=
                (v_labeling_facts->>'unfinished_effective_work_count')::bigint;
            IF v_requested_role IN ('super_admin', 'project_admin', 'viewer')
               AND v_non_viewer_membership_count > 0 THEN
                v_blockers := pg_catalog.array_append(
                    v_blockers,
                    '账号仍持有标注/质检项目职责，请先在项目中变更职责'
                );
            END IF;
            IF v_requested_role NOT IN ('super_admin', 'project_admin')
               AND v_owned_project_count > 0 THEN
                v_blockers := pg_catalog.array_append(
                    v_blockers,
                    '账号仍是项目负责人，请先转移项目后变更平台角色'
                );
            END IF;
            IF v_requested_role = 'viewer' AND v_unfinished_count > 0 THEN
                v_blockers := pg_catalog.array_append(
                    v_blockers,
                    '账号仍有未完成的标注/质检工作，请先交接'
                );
            END IF;
        END IF;

        WITH workspace_projects AS (
            SELECT project.value, project.ordinality
            FROM pg_catalog.jsonb_array_elements(
                v_workspace_facts->'membership_projects'
            ) WITH ORDINALITY AS project(value, ordinality)
        ),
        labeling_projects AS (
            SELECT project.value, project.ordinality
            FROM pg_catalog.jsonb_array_elements(
                v_labeling_facts->'membership_projects'
            ) WITH ORDINALITY AS project(value, ordinality)
        ),
        joined_projects AS (
            SELECT workspace_projects.ordinality,
                   workspace_projects.value AS workspace_project,
                   labeling_projects.value AS labeling_project,
                   COALESCE((labeling_projects.value->>'annotator_batch_count')::bigint, 0::bigint)
                     AS annotator_batch_count,
                   COALESCE((labeling_projects.value->>'reviewer_batch_count')::bigint, 0::bigint)
                     AS reviewer_batch_count,
                   COALESCE((labeling_projects.value->>'assigned_task_count')::bigint, 0::bigint)
                     AS assigned_task_count,
                   COALESCE((labeling_projects.value->>'review_task_count')::bigint, 0::bigint)
                     AS review_task_count
            FROM workspace_projects
            LEFT JOIN labeling_projects
              ON labeling_projects.value->>'project_id' =
                 workspace_projects.value->>'project_id'
        )
        SELECT COALESCE(
                   pg_catalog.jsonb_agg(
                       pg_catalog.jsonb_build_object(
                           'project_id', joined_projects.workspace_project->'project_id',
                           'project_name', joined_projects.workspace_project->'project_name',
                           'membership_role', joined_projects.workspace_project->'membership_role',
                           'annotator_batch_count', joined_projects.annotator_batch_count,
                           'reviewer_batch_count', joined_projects.reviewer_batch_count,
                           'assigned_task_count', joined_projects.assigned_task_count,
                           'review_task_count', joined_projects.review_task_count
                       ) ORDER BY joined_projects.ordinality
                   ),
                   '[]'::jsonb
               ),
               COALESCE(pg_catalog.sum(
                   joined_projects.annotator_batch_count + joined_projects.reviewer_batch_count
               ), 0)::bigint,
               COALESCE(pg_catalog.sum(joined_projects.assigned_task_count), 0)::bigint,
               COALESCE(pg_catalog.sum(joined_projects.review_task_count), 0)::bigint
        INTO v_project_items, v_assigned_batch_count,
             v_assigned_task_count, v_review_task_count
        FROM joined_projects;

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-platform-roles-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', pg_catalog.jsonb_build_object(
                'user_id', v_target_user_id,
                'email', v_user_record->'email',
                'current_role', v_target_role,
                'requested_role', v_requested_role,
                'can_change', pg_catalog.cardinality(v_blockers) = 0,
                'blockers', pg_catalog.to_jsonb(v_blockers),
                'projects', v_project_items,
                'assigned_batch_count', v_assigned_batch_count,
                'assigned_task_count', v_assigned_task_count,
                'review_task_count', v_review_task_count,
                'other_project_count', 0,
                'warnings', pg_catalog.jsonb_build_array(
                    '平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。',
                    '若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。'
                )
            ),
            'replayed', false
        );
    END IF;

    SELECT account.email, account.role, account.is_active
    INTO v_target_email, v_target_role, v_target_active
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    IF NOT FOUND OR v_target_active IS DISTINCT FROM true THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'target_not_found', 404, to_jsonb('用户不存在'::text)
        );
    END IF;
    IF v_target_user_id = v_actor_id THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'self_change', 400, to_jsonb('不能修改自己的角色'::text)
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-platform-role-operation/v1:' || v_operation_id::text,
            0
        )
    ) THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'account_busy', 409, to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    SELECT receipt.operation, receipt.actor_id, receipt.target_user_id,
           receipt.request_fingerprint, receipt.derivation_id, receipt.result
    INTO v_receipt
    FROM public.identity_platform_role_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt.operation IS DISTINCT FROM 'change_platform_role'
           OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_platform_roles_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-platform-roles-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_receipt.result,
            'replayed', true
        );
    END IF;

    IF v_target_role = v_requested_role THEN
        v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
        IF v_user_record IS NULL THEN
            RETURN aap_identity_platform_roles_v1._failure('unavailable', 503, NULL);
        END IF;
        v_result := pg_catalog.jsonb_build_object('user', v_user_record);
        INSERT INTO public.identity_platform_role_operation_receipts (
            operation_id, actor_id, target_user_id, operation,
            request_fingerprint, derivation_id, result, audit_id
        ) VALUES (
            v_operation_id, v_actor_id, v_target_user_id, 'change_platform_role',
            v_request_fingerprint, v_derivation_id, v_result, NULL
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-platform-roles-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_result,
            'replayed', false
        );
    END IF;

    BEGIN
        v_project_lock_acquired :=
            aap_workspace_v1.lock_identity_platform_role_owned_projects(
                v_target_user_id
            );
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'project_busy', 409, pg_catalog.jsonb_build_object('reason', 'project_busy')
        );
    END;
    IF v_project_lock_acquired IS DISTINCT FROM true THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'project_busy', 409, pg_catalog.jsonb_build_object('reason', 'project_busy')
        );
    END IF;

    SELECT * INTO v_snapshot
    FROM aap_identity_platform_roles_v1._preview_snapshot(v_target_user_id);
    IF NOT FOUND THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'target_not_found', 404, to_jsonb('用户不存在'::text)
        );
    END IF;
    v_workspace_facts := v_snapshot.workspace_facts;
    v_labeling_facts := v_snapshot.labeling_facts;
    v_non_viewer_membership_count :=
        (v_workspace_facts->>'non_viewer_membership_count')::bigint;
    v_owned_project_count :=
        (v_workspace_facts->>'owned_project_count')::bigint;
    v_unfinished_count :=
        (v_labeling_facts->>'unfinished_effective_work_count')::bigint;

    IF v_requested_role IN ('super_admin', 'project_admin', 'viewer')
       AND v_non_viewer_membership_count > 0 THEN
        v_blockers := pg_catalog.array_append(
            v_blockers,
            '账号仍持有标注/质检项目职责，请先在项目中变更职责'
        );
    END IF;
    IF v_requested_role NOT IN ('super_admin', 'project_admin')
       AND v_owned_project_count > 0 THEN
        v_blockers := pg_catalog.array_append(
            v_blockers,
            '账号仍是项目负责人，请先转移项目后变更平台角色'
        );
    END IF;
    IF v_requested_role = 'viewer' AND v_unfinished_count > 0 THEN
        v_blockers := pg_catalog.array_append(
            v_blockers,
            '账号仍有未完成的标注/质检工作，请先交接'
        );
    END IF;
    IF pg_catalog.cardinality(v_blockers) > 0 THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'platform_role_change_blocked', 409,
            pg_catalog.jsonb_build_object(
                'reason', 'platform_role_change_blocked',
                'blockers', pg_catalog.to_jsonb(v_blockers)
            )
        );
    END IF;

    IF v_target_role = 'super_admin' AND v_requested_role <> 'super_admin' THEN
        v_super_admin_status := aap_identity_v1.super_admin_status(
            v_target_user_id
        );
        IF (v_super_admin_status->>'other_unreserved')::bigint = 0 THEN
            RETURN aap_identity_platform_roles_v1._failure(
                'last_super_admin', 400,
                to_jsonb('不能降级最后一名超级管理员'::text)
            );
        END IF;
    END IF;

    UPDATE public.users AS account
    SET role = v_requested_role
    WHERE account.id = v_target_user_id;
    v_audit_id := aap_audit_v1.record_identity_platform_role_change(
        pg_catalog.jsonb_build_object(
            'actor_id', v_principal->'actor_id',
            'email', v_principal->'email',
            'role', v_principal->'role'
        ),
        v_target_user_id,
        v_context,
        v_target_email,
        v_target_role,
        v_requested_role
    );
    v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_user_record IS NULL
       OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
       OR v_user_record->>'role' IS DISTINCT FROM v_requested_role
       OR v_user_record->'is_active' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid frozen platform-role user result';
    END IF;
    v_result := pg_catalog.jsonb_build_object('user', v_user_record);
    INSERT INTO public.identity_platform_role_operation_receipts (
        operation_id, actor_id, target_user_id, operation,
        request_fingerprint, derivation_id, result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_target_user_id, 'change_platform_role',
        v_request_fingerprint, v_derivation_id, v_result, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-platform-roles-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_result,
        'replayed', false
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_platform_roles_v1._failure(
            'account_busy', 409, to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    WHEN OTHERS THEN
        RETURN aap_identity_platform_roles_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_platform_roles_v1.execute_platform_role_command(
    jsonb
) FROM PUBLIC;
