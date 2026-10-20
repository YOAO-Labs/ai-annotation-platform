-- Native Workspace-owned project membership commands. Policy and canonical
-- resource snapshots remain in the portable Workspace manager.
CREATE FUNCTION aap_workspace_membership_v1._has_exact_keys(
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
                 SELECT 1
                 FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
                 WHERE NOT (field.name = ANY(p_keys))
             )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_membership_v1._failure(
    p_status_code integer,
    p_detail jsonb,
    p_headers jsonb DEFAULT NULL,
    p_rollback boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-membership-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'status_code', p_status_code,
            'detail', p_detail,
            'headers', p_headers,
            'rollback', p_rollback
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1._failure(
    integer, jsonb, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_membership_v1._request_is_valid(
    p_request jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_payload jsonb;
    v_context jsonb;
BEGIN
    IF NOT aap_workspace_membership_v1._has_exact_keys(
        p_request,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor_id', 'project_id',
            'payload', 'audit_context'
        ]::text[]
    ) THEN
        RETURN false;
    END IF;

    IF pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-membership/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
       OR (p_request->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'project_id') IS DISTINCT FROM 'string'
       OR (p_request->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR p_request->>'operation' NOT IN (
           'add_member', 'preview_role_change', 'change_role', 'remove_member'
       ) THEN
        RETURN false;
    END IF;

    v_operation := p_request->>'operation';
    v_payload := p_request->'payload';
    v_context := p_request->'audit_context';
    IF NOT aap_workspace_membership_v1._has_exact_keys(
        v_context,
        ARRAY['method', 'path', 'ip', 'request_id']::text[]
    ) THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_each(v_context) AS field(name, value)
        WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
    ) THEN
        RETURN false;
    END IF;
    IF NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN false;
    END IF;

    IF v_operation = 'add_member' THEN
        RETURN aap_workspace_membership_v1._has_exact_keys(
                   v_payload,
                   ARRAY['target_user_id', 'project_role']::text[]
               )
           AND pg_catalog.jsonb_typeof(v_payload->'target_user_id') = 'string'
           AND (v_payload->>'target_user_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND pg_catalog.jsonb_typeof(v_payload->'project_role') = 'string';
    ELSIF v_operation = 'preview_role_change' THEN
        RETURN aap_workspace_membership_v1._has_exact_keys(
                   v_payload,
                   ARRAY[
                       'member_id', 'target_role', 'replacement_annotator_id',
                       'replacement_reviewer_id'
                   ]::text[]
               )
           AND pg_catalog.jsonb_typeof(v_payload->'member_id') = 'string'
           AND (v_payload->>'member_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND pg_catalog.jsonb_typeof(v_payload->'target_role') = 'string'
           AND pg_catalog.jsonb_typeof(v_payload->'replacement_annotator_id') IN ('string', 'null')
           AND (
               pg_catalog.jsonb_typeof(v_payload->'replacement_annotator_id') = 'null'
               OR (v_payload->>'replacement_annotator_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           )
           AND pg_catalog.jsonb_typeof(v_payload->'replacement_reviewer_id') IN ('string', 'null')
           AND (
               pg_catalog.jsonb_typeof(v_payload->'replacement_reviewer_id') = 'null'
               OR (v_payload->>'replacement_reviewer_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           );
    ELSIF v_operation = 'change_role' THEN
        RETURN aap_workspace_membership_v1._has_exact_keys(
                   v_payload,
                   ARRAY[
                       'member_id', 'target_role', 'replacement_annotator_id',
                       'replacement_reviewer_id', 'expected_version',
                       'preview_token', 'reason'
                   ]::text[]
               )
           AND pg_catalog.jsonb_typeof(v_payload->'member_id') = 'string'
           AND (v_payload->>'member_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND pg_catalog.jsonb_typeof(v_payload->'target_role') = 'string'
           AND pg_catalog.jsonb_typeof(v_payload->'replacement_annotator_id') IN ('string', 'null')
           AND (
               pg_catalog.jsonb_typeof(v_payload->'replacement_annotator_id') = 'null'
               OR (v_payload->>'replacement_annotator_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           )
           AND pg_catalog.jsonb_typeof(v_payload->'replacement_reviewer_id') IN ('string', 'null')
           AND (
               pg_catalog.jsonb_typeof(v_payload->'replacement_reviewer_id') = 'null'
               OR (v_payload->>'replacement_reviewer_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           )
           AND pg_catalog.jsonb_typeof(v_payload->'expected_version') = 'number'
           AND (v_payload->>'expected_version') ~ '^-?(0|[1-9][0-9]*)$'
           AND pg_catalog.jsonb_typeof(v_payload->'preview_token') = 'string'
           AND pg_catalog.jsonb_typeof(v_payload->'reason') = 'string';
    ELSE
        RETURN aap_workspace_membership_v1._has_exact_keys(
                   v_payload,
                   ARRAY['member_id']::text[]
               )
           AND pg_catalog.jsonb_typeof(v_payload->'member_id') = 'string'
           AND (v_payload->>'member_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1._request_is_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_membership_v1._membership_record(
    p_project_id uuid,
    p_member_id uuid
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', member.id,
        'project_id', member.project_id,
        'user_id', member.user_id,
        'role', member.role,
        'assigned_by', member.assigned_by,
        'version', member.version,
        'assigned_at', member.assigned_at,
        'updated_at', member.updated_at,
        'weekly_target', member.weekly_target
    )
    FROM public.project_members AS member
    WHERE member.project_id = p_project_id
      AND member.id = p_member_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1._membership_record(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_membership_v1.membership_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-membership-native/v1',
        'operations', ARRAY[
            'add_member', 'preview_role_change', 'change_role', 'remove_member'
        ]::text[]
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1.membership_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_membership_v1.prepare_membership_command(
    p_request jsonb
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
    v_actor_id uuid;
    v_project_id uuid;
    v_payload jsonb;
    v_member_id uuid;
    v_member_user_id uuid;
    v_project_role text;
    v_target_role text;
    v_expected_version numeric;
    v_replacement_annotator_id uuid;
    v_replacement_reviewer_id uuid;
    v_is_preview boolean;
    v_is_removal boolean;
    v_request_hash text;
    v_tx_id text;
    v_facts_hash text;
    v_binding text;
    v_receipt record;
    v_actor record;
    v_project record;
    v_locked_accounts jsonb;
    v_locked_actor record;
    v_locked_target record;
    v_lock_account_ids uuid[] := ARRAY[]::uuid[];
    v_account_ids uuid[] := ARRAY[]::uuid[];
    v_requested_account_ids uuid[] := ARRAY[]::uuid[];
    v_candidate_reviewer_ids uuid[] := ARRAY[]::uuid[];
    v_eligible_reviewer_ids uuid[] := ARRAY[]::uuid[];
    v_receiver_ids uuid[] := ARRAY[]::uuid[];
    v_accounts jsonb := '[]'::jsonb;
    v_actor_json jsonb;
    v_project_json jsonb;
    v_member_json jsonb;
    v_receiver_memberships jsonb := '[]'::jsonb;
    v_label_lock jsonb;
    v_labeling jsonb;
    v_data jsonb;
BEGIN
    IF aap_workspace_membership_v1._request_is_valid(p_request) IS DISTINCT FROM true THEN
        RETURN aap_workspace_membership_v1._failure(
            400, pg_catalog.to_jsonb('invalid_request'::text)
        );
    END IF;

    v_operation := p_request->>'operation';
    v_operation_id := (p_request->>'operation_id')::uuid;
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_project_id := (p_request->>'project_id')::uuid;
    v_payload := p_request->'payload';
    v_is_preview := v_operation = 'preview_role_change';
    v_is_removal := v_operation = 'remove_member';
    v_request_hash := pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(p_request::text, 'UTF8')),
        'hex'
    );

    IF v_operation IN ('preview_role_change', 'change_role') THEN
        v_target_role := v_payload->>'target_role';
        IF v_target_role NOT IN ('annotator', 'reviewer', 'viewer') THEN
            RETURN aap_workspace_membership_v1._failure(
                400, pg_catalog.to_jsonb('非法项目职责'::text)
            );
        END IF;
        v_replacement_annotator_id := NULLIF(
            v_payload->>'replacement_annotator_id', ''
        )::uuid;
        IF pg_catalog.jsonb_typeof(v_payload->'replacement_annotator_id') = 'null' THEN
            v_replacement_annotator_id := NULL;
        END IF;
        v_replacement_reviewer_id := NULLIF(
            v_payload->>'replacement_reviewer_id', ''
        )::uuid;
        IF pg_catalog.jsonb_typeof(v_payload->'replacement_reviewer_id') = 'null' THEN
            v_replacement_reviewer_id := NULL;
        END IF;
    END IF;
    IF v_operation = 'change_role' THEN
        v_expected_version := (v_payload->>'expected_version')::numeric;
    END IF;
    IF v_operation IN ('preview_role_change', 'change_role') THEN
        IF v_replacement_annotator_id IS NOT NULL THEN
            v_receiver_ids := pg_catalog.array_append(
                v_receiver_ids, v_replacement_annotator_id
            );
        END IF;
        IF v_replacement_reviewer_id IS NOT NULL THEN
            v_receiver_ids := pg_catalog.array_append(
                v_receiver_ids, v_replacement_reviewer_id
            );
        END IF;
    END IF;
    IF v_operation = 'add_member' THEN
        v_member_user_id := (v_payload->>'target_user_id')::uuid;
        v_project_role := v_payload->>'project_role';
        IF v_project_role NOT IN ('annotator', 'reviewer', 'viewer') THEN
            RETURN aap_workspace_membership_v1._failure(
                400, pg_catalog.to_jsonb('非法项目职责'::text)
            );
        END IF;
    ELSIF v_operation IN (
        'preview_role_change', 'change_role', 'remove_member'
    ) THEN
        v_member_id := (v_payload->>'member_id')::uuid;
    END IF;

    -- A rejected policy can leave the caller transaction open. A new prepare
    -- supersedes the unapplied binding; the old apply then fails its binding.
    IF NOT v_is_preview THEN
        PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
    END IF;

    SELECT receipt.operation_id, receipt.actor_id, receipt.project_id,
           receipt.operation, receipt.request_hash, receipt.result
    INTO v_receipt
    FROM public.workspace_membership_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    IF FOUND THEN
        SELECT account.id, account.name, account.email, account.role,
               account.is_active
        INTO v_actor
        FROM aap_identity_v1.read_workspace_membership_accounts(
            ARRAY[v_actor_id]::uuid[]
        ) AS account
        WHERE account.id = v_actor_id;
        IF NOT FOUND OR v_actor.is_active IS NOT TRUE THEN
            RETURN aap_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
        SELECT project.id, project.owner_id
        INTO v_project
        FROM public.projects AS project
        WHERE project.id = v_project_id;
        IF NOT FOUND THEN
            RETURN aap_workspace_membership_v1._failure(
                404, pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;
        IF v_actor.role <> 'super_admin'
           AND NOT (v_actor.role = 'project_admin'
                    AND v_project.owner_id = v_actor_id) THEN
            RETURN aap_workspace_membership_v1._failure(
                403,
                pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行成员变更'::text)
            );
        END IF;
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.project_id IS DISTINCT FROM v_project_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.request_hash IS DISTINCT FROM v_request_hash THEN
            RETURN aap_workspace_membership_v1._failure(
                409,
                pg_catalog.jsonb_build_object('reason', 'operation_conflict')
            );
        END IF;
        IF pg_catalog.jsonb_typeof(v_receipt.result) IS DISTINCT FROM 'object'
           OR v_receipt.result->>'schema' IS DISTINCT FROM 'aap-workspace-membership/v1'
           OR v_receipt.result->'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_receipt.result->>'operation_id' IS DISTINCT FROM v_operation_id::text
           OR v_receipt.result->>'operation' IS DISTINCT FROM v_operation
           OR v_receipt.result->>'actor_id' IS DISTINCT FROM v_actor_id::text
           OR v_receipt.result->>'project_id' IS DISTINCT FROM v_project_id::text THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace membership success receipt';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-membership-native/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'kind', 'replay',
                'operation_id', v_operation_id,
                'operation', v_operation,
                'actor_id', v_actor_id,
                'project_id', v_project_id,
                'reply', v_receipt.result
            )
        );
    END IF;

    IF v_operation IN ('preview_role_change', 'change_role', 'remove_member') THEN
        SELECT member.id, member.user_id, member.role, member.version
        INTO v_member_id, v_member_user_id, v_project_role, v_expected_version
        FROM public.project_members AS member
        WHERE member.project_id = v_project_id
          AND member.id = v_member_id;
        IF NOT FOUND THEN
            RETURN aap_workspace_membership_v1._failure(
                404, pg_catalog.to_jsonb('成员不存在'::text)
            );
        END IF;
    END IF;

    IF v_is_preview THEN
        SELECT project.id, project.owner_id
        INTO v_project
        FROM public.projects AS project
        WHERE project.id = v_project_id;
        IF NOT FOUND THEN
            RETURN aap_workspace_membership_v1._failure(
                404, pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;
        SELECT account.id, account.name, account.email, account.role,
               account.is_active
        INTO v_actor
        FROM aap_identity_v1.read_workspace_membership_accounts(
            ARRAY[v_actor_id]::uuid[]
        ) AS account
        WHERE account.id = v_actor_id;
        IF NOT FOUND OR v_actor.is_active IS NOT TRUE THEN
            RETURN aap_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
        IF v_actor.role <> 'super_admin'
           AND NOT (v_actor.role = 'project_admin'
                    AND v_project.owner_id = v_actor_id) THEN
            RETURN aap_workspace_membership_v1._failure(
                403,
                pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行成员变更'::text)
            );
        END IF;
    ELSE
        IF v_operation = 'add_member' THEN
            v_lock_account_ids := ARRAY[v_actor_id, v_member_user_id]::uuid[];
        ELSE
            v_lock_account_ids := ARRAY[v_actor_id, v_member_user_id]::uuid[];
            IF v_operation = 'change_role' THEN
                IF v_replacement_annotator_id IS NOT NULL THEN
                    v_lock_account_ids := pg_catalog.array_append(
                        v_lock_account_ids, v_replacement_annotator_id
                    );
                END IF;
                IF v_replacement_reviewer_id IS NOT NULL THEN
                    v_lock_account_ids := pg_catalog.array_append(
                        v_lock_account_ids, v_replacement_reviewer_id
                    );
                END IF;
            END IF;
        END IF;
        SELECT COALESCE(
            pg_catalog.array_agg(DISTINCT item.id ORDER BY item.id),
            ARRAY[]::uuid[]
        ) INTO v_lock_account_ids
        FROM pg_catalog.unnest(v_lock_account_ids) AS item(id);

        BEGIN
            v_locked_accounts := aap_identity_v1.lock_accounts_update(
                v_lock_account_ids, true
            );
        EXCEPTION
            WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
                RETURN aap_workspace_membership_v1._failure(
                    409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text),
                    NULL,
                    true
                );
        END;
        IF pg_catalog.jsonb_typeof(v_locked_accounts) IS DISTINCT FROM 'object'
           OR v_locked_accounts->>'schema' IS DISTINCT FROM 'aap-identity-account-locks/v1'
           OR pg_catalog.jsonb_typeof(v_locked_accounts->'accounts') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace membership account lock result';
        END IF;

        BEGIN
            SELECT project.id, project.owner_id
            INTO v_project
            FROM public.projects AS project
            WHERE project.id = v_project_id
            FOR UPDATE OF project NOWAIT;
        EXCEPTION
            WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
                RETURN aap_workspace_membership_v1._failure(
                    409,
                    pg_catalog.jsonb_build_object(
                        'reason', 'resource_busy',
                        'detail', '项目资源正在变更，请刷新后重试'
                    ),
                    NULL,
                    true
                );
        END;
        IF NOT FOUND THEN
            RETURN aap_workspace_membership_v1._failure(
                404, pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;

        SELECT account.id, account.name, account.email, account.role,
               account.is_active
        INTO v_locked_actor
        FROM pg_catalog.jsonb_to_recordset(v_locked_accounts->'accounts') AS account(
            id uuid, name text, email text, role text, is_active boolean
        )
        WHERE account.id = v_actor_id;
        IF NOT FOUND OR v_locked_actor.is_active IS NOT TRUE THEN
            RETURN aap_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
        IF v_locked_actor.role <> 'super_admin'
           AND NOT (v_locked_actor.role = 'project_admin'
                    AND v_project.owner_id = v_actor_id) THEN
            RETURN aap_workspace_membership_v1._failure(
                403,
                pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行成员变更'::text)
            );
        END IF;

        IF v_operation IN ('change_role', 'remove_member') THEN
            BEGIN
                SELECT member.id, member.user_id, member.role, member.version
                INTO v_member_id, v_member_user_id, v_project_role, v_expected_version
                FROM public.project_members AS member
                WHERE member.project_id = v_project_id
                  AND member.id = v_member_id
                FOR UPDATE OF member NOWAIT;
            EXCEPTION
                WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
                    RETURN aap_workspace_membership_v1._failure(
                        409,
                        pg_catalog.jsonb_build_object(
                            'reason', 'membership_busy',
                            'detail', '成员正在变更，请刷新后重试'
                        ),
                        NULL,
                        true
                    );
            END;
            IF NOT FOUND THEN
                RETURN aap_workspace_membership_v1._failure(
                    404, pg_catalog.to_jsonb('成员不存在'::text)
                );
            END IF;
        END IF;

        IF v_operation = 'change_role' THEN
            IF v_expected_version IS DISTINCT FROM
               (v_payload->>'expected_version')::numeric THEN
                RETURN aap_workspace_membership_v1._failure(
                    409,
                    pg_catalog.jsonb_build_object(
                        'reason', 'stale_member_version',
                        'current_version', v_expected_version
                    )
                );
            END IF;
            SELECT account.id, account.role, account.is_active
            INTO v_locked_target
            FROM pg_catalog.jsonb_to_recordset(v_locked_accounts->'accounts') AS account(
                id uuid, role text, is_active boolean
            )
            WHERE account.id = v_member_user_id;
            IF NOT FOUND OR v_locked_target.is_active IS NOT TRUE THEN
                RETURN aap_workspace_membership_v1._failure(
                    409,
                    pg_catalog.to_jsonb('目标成员账号已停用，请刷新后重试'::text)
                );
            END IF;
        ELSIF v_operation = 'add_member' THEN
            SELECT account.id, account.role, account.is_active
            INTO v_locked_target
            FROM pg_catalog.jsonb_to_recordset(v_locked_accounts->'accounts') AS account(
                id uuid, role text, is_active boolean
            )
            WHERE account.id = v_member_user_id;
            IF NOT FOUND OR v_locked_target.is_active IS NOT TRUE THEN
                RETURN aap_workspace_membership_v1._failure(
                    404, pg_catalog.to_jsonb('目标用户不存在'::text)
                );
            END IF;
            IF NOT (
                (v_locked_target.role = 'viewer' AND v_project_role = 'viewer')
                OR (v_locked_target.role = 'employee'
                    AND v_project_role IN ('annotator', 'reviewer', 'viewer'))
            ) THEN
                RETURN aap_workspace_membership_v1._failure(
                    400,
                    pg_catalog.to_jsonb(
                        '账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'::text
                    )
                );
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.project_members AS member
                WHERE member.project_id = v_project_id
                  AND member.user_id = v_member_user_id
            ) THEN
                RETURN aap_workspace_membership_v1._failure(
                    409, pg_catalog.to_jsonb('该用户已在项目中'::text)
                );
            END IF;
        END IF;

        IF v_operation = 'change_role' THEN
            v_receiver_ids := ARRAY[]::uuid[];
            IF v_replacement_annotator_id IS NOT NULL THEN
                v_receiver_ids := pg_catalog.array_append(
                    v_receiver_ids, v_replacement_annotator_id
                );
            END IF;
            IF v_replacement_reviewer_id IS NOT NULL THEN
                v_receiver_ids := pg_catalog.array_append(
                    v_receiver_ids, v_replacement_reviewer_id
                );
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(DISTINCT item.id ORDER BY item.id),
                ARRAY[]::uuid[]
            ) INTO v_receiver_ids
            FROM pg_catalog.unnest(v_receiver_ids) AS item(id);
            BEGIN
                PERFORM member.id
                FROM public.project_members AS member
                WHERE member.project_id = v_project_id
                  AND member.user_id = ANY(v_receiver_ids)
                ORDER BY member.user_id
                FOR SHARE OF member NOWAIT;
            EXCEPTION
                WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
                    RETURN aap_workspace_membership_v1._failure(
                        409,
                        pg_catalog.jsonb_build_object(
                            'reason', 'membership_busy',
                            'detail', '接收人成员关系正在变更，请刷新后重试'
                        ),
                        NULL,
                        true
                    );
            END;
        END IF;
    END IF;

    SELECT project.id, project.owner_id
    INTO v_project
    FROM public.projects AS project
    WHERE project.id = v_project_id;
    IF NOT FOUND THEN
        RETURN aap_workspace_membership_v1._failure(
            404, pg_catalog.to_jsonb('项目不存在'::text)
        );
    END IF;

    IF v_operation IN ('preview_role_change', 'change_role', 'remove_member') THEN
        SELECT COALESCE(
            pg_catalog.array_agg(member.user_id ORDER BY member.user_id),
            ARRAY[]::uuid[]
        ) INTO v_candidate_reviewer_ids
        FROM public.project_members AS member
        WHERE member.project_id = v_project_id
          AND member.role = 'reviewer';
        v_requested_account_ids := ARRAY[v_member_user_id]::uuid[];
        IF v_replacement_annotator_id IS NOT NULL THEN
            v_requested_account_ids := pg_catalog.array_append(
                v_requested_account_ids, v_replacement_annotator_id
            );
        END IF;
        IF v_replacement_reviewer_id IS NOT NULL THEN
            v_requested_account_ids := pg_catalog.array_append(
                v_requested_account_ids, v_replacement_reviewer_id
            );
        END IF;
        v_account_ids := ARRAY[v_actor_id]::uuid[]
            || v_requested_account_ids || v_candidate_reviewer_ids;
    ELSE
        v_requested_account_ids := ARRAY[v_actor_id, v_member_user_id]::uuid[];
        v_account_ids := v_requested_account_ids;
    END IF;
    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT item.id ORDER BY item.id),
        ARRAY[]::uuid[]
    ) INTO v_account_ids
    FROM pg_catalog.unnest(v_account_ids) AS item(id);
    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT item.id ORDER BY item.id),
        ARRAY[]::uuid[]
    ) INTO v_requested_account_ids
    FROM pg_catalog.unnest(v_requested_account_ids) AS item(id);

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', account.id,
                'role', account.role,
                'is_active', account.is_active
            ) ORDER BY account.id
        ),
        '[]'::jsonb
    ) INTO v_accounts
    FROM aap_identity_v1.read_workspace_membership_accounts(v_account_ids) AS account
    WHERE account.id = ANY(v_requested_account_ids);

    SELECT account.id, account.name, account.email, account.role,
           account.is_active
    INTO v_actor
    FROM aap_identity_v1.read_workspace_membership_accounts(v_account_ids) AS account
    WHERE account.id = v_actor_id;
    IF NOT FOUND OR v_actor.is_active IS NOT TRUE THEN
        RETURN aap_workspace_membership_v1._failure(
            401, pg_catalog.to_jsonb('账号已停用'::text)
        );
    END IF;
    IF v_actor.role <> 'super_admin'
       AND NOT (v_actor.role = 'project_admin' AND v_project.owner_id = v_actor_id) THEN
        RETURN aap_workspace_membership_v1._failure(
            403,
            pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行成员变更'::text)
        );
    END IF;
    v_actor_json := pg_catalog.jsonb_build_object(
        'id', v_actor.id,
        'name', v_actor.name,
        'email', v_actor.email,
        'role', v_actor.role,
        'is_active', v_actor.is_active
    );
    v_project_json := pg_catalog.jsonb_build_object(
        'id', v_project.id,
        'owner_id', v_project.owner_id
    );
    v_member_json := NULL;
    v_labeling := NULL;
    IF v_operation IN ('preview_role_change', 'change_role', 'remove_member') THEN
        v_member_json := aap_workspace_membership_v1._membership_record(
            v_project_id, v_member_id
        );
        IF v_member_json IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace membership row disappeared under its lock';
        END IF;
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', member.id,
                    'project_id', member.project_id,
                    'user_id', member.user_id,
                    'role', member.role,
                    'version', member.version
                ) ORDER BY member.user_id
            ),
            '[]'::jsonb
        ) INTO v_receiver_memberships
        FROM public.project_members AS member
        WHERE member.project_id = v_project_id
          AND (
              (member.user_id = v_replacement_annotator_id
               AND member.role = 'annotator')
              OR (member.user_id = v_replacement_reviewer_id
                  AND member.role = 'reviewer')
          );

        IF v_operation = 'preview_role_change' THEN
            v_labeling := aap_labeling_v1.read_membership_work(
                v_project_id,
                v_member_user_id,
                v_replacement_annotator_id,
                v_replacement_reviewer_id
            );
        ELSE
            v_label_lock := aap_labeling_v1.lock_membership_work(
                v_project_id,
                v_member_user_id,
                CASE WHEN v_is_removal THEN 'viewer' ELSE v_target_role END,
                v_is_removal
            );
            IF pg_catalog.jsonb_typeof(v_label_lock) IS DISTINCT FROM 'object'
               OR v_label_lock->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                IF NOT aap_workspace_membership_v1._has_exact_keys(
                    v_label_lock, ARRAY['ok', 'error']::text[]
                ) OR v_label_lock->'ok' IS DISTINCT FROM 'false'::jsonb
                  OR NOT aap_workspace_membership_v1._has_exact_keys(
                      v_label_lock->'error', ARRAY['reason', 'detail']::text[]
                  ) OR v_label_lock #>> '{error,reason}' IS DISTINCT FROM 'resource_busy'
                  OR pg_catalog.jsonb_typeof(v_label_lock #> '{error,detail}') IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'invalid Labeling membership lock result';
                END IF;
                RETURN aap_workspace_membership_v1._failure(
                    409,
                    pg_catalog.jsonb_build_object(
                        'reason', 'resource_busy',
                        'detail', v_label_lock #> '{error,detail}'
                    ),
                    NULL,
                    true
                );
            END IF;
            IF NOT aap_workspace_membership_v1._has_exact_keys(
                v_label_lock, ARRAY['ok']::text[]
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Labeling membership lock success';
            END IF;
            v_labeling := aap_labeling_v1.read_membership_work(
                v_project_id,
                v_member_user_id,
                v_replacement_annotator_id,
                v_replacement_reviewer_id
            );
        END IF;
        IF pg_catalog.jsonb_typeof(v_labeling) IS DISTINCT FROM 'object'
           OR v_labeling->>'schema' IS DISTINCT FROM 'aap-labeling-membership-work/v1'
           OR v_labeling->>'project_id' IS DISTINCT FROM v_project_id::text
           OR v_labeling->>'user_id' IS DISTINCT FROM v_member_user_id::text THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Labeling membership work facts';
        END IF;
        SELECT COALESCE(
            pg_catalog.array_agg(account.id ORDER BY account.id),
            ARRAY[]::uuid[]
        ) INTO v_eligible_reviewer_ids
        FROM aap_identity_v1.read_workspace_membership_accounts(
            v_candidate_reviewer_ids
        ) AS account
        WHERE account.is_active IS TRUE
          AND account.role = 'employee';
        v_receiver_ids := ARRAY[]::uuid[];
        IF v_replacement_annotator_id IS NOT NULL THEN
            v_receiver_ids := pg_catalog.array_append(
                v_receiver_ids, v_replacement_annotator_id
            );
        END IF;
        IF v_replacement_reviewer_id IS NOT NULL THEN
            v_receiver_ids := pg_catalog.array_append(
                v_receiver_ids, v_replacement_reviewer_id
            );
        END IF;
        IF pg_catalog.cardinality(v_receiver_ids) = 0 THEN
            v_receiver_memberships := '[]'::jsonb;
        END IF;
    ELSE
        v_receiver_memberships := '[]'::jsonb;
        v_eligible_reviewer_ids := ARRAY[]::uuid[];
    END IF;

    IF v_operation = 'add_member' THEN
        SELECT account.id, account.name, account.email, account.role,
               account.is_active
        INTO v_actor
        FROM aap_identity_v1.read_workspace_membership_accounts(v_account_ids) AS account
        WHERE account.id = v_actor_id;
        IF NOT FOUND OR v_actor.is_active IS NOT TRUE THEN
            RETURN aap_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
    END IF;

    IF v_is_preview THEN
        v_tx_id := COALESCE(
            pg_catalog.txid_current_if_assigned()::text,
            'read:' || pg_catalog.pg_backend_pid()::text || ':'
                || pg_catalog.statement_timestamp()::text
        );
    ELSE
        v_tx_id := pg_catalog.txid_current()::text;
    END IF;
    v_data := pg_catalog.jsonb_build_object(
        'kind', 'facts',
        'operation_id', v_operation_id,
        'operation', v_operation,
        'actor_id', v_actor_id,
        'project_id', v_project_id,
        'transaction_id', v_tx_id,
        'actor', v_actor_json,
        'project', v_project_json,
        'member', v_member_json,
        'accounts', v_accounts,
        'receiver_memberships', v_receiver_memberships,
        'eligible_reviewer_ids', v_eligible_reviewer_ids,
        'labeling', v_labeling
    );
    v_facts_hash := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to((v_data - 'transaction_id')::text, 'UTF8')
        ),
        'hex'
    );
    IF NOT v_is_preview THEN
        PERFORM pg_catalog.set_config(
            'aap.workspace_membership_prepare',
            pg_catalog.jsonb_build_object(
                'transaction_id', v_tx_id,
                'operation_id', v_operation_id,
                'request_hash', v_request_hash,
                'facts_hash', v_facts_hash
            )::text,
            true
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-membership-native/v1',
        'ok', true,
        'data', v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1.prepare_membership_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_membership_v1.apply_membership_command(
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
    v_request jsonb;
    v_operation text;
    v_operation_id uuid;
    v_actor_id uuid;
    v_project_id uuid;
    v_request_hash text;
    v_transaction_id text;
    v_binding_text text;
    v_binding jsonb;
    v_revalidated jsonb;
    v_revalidated_data jsonb;
    v_revalidated_binding_text text;
    v_revalidated_binding jsonb;
    v_facts_hash text;
    v_target_user_id uuid;
    v_target_role text;
    v_replacement_annotator_id uuid;
    v_replacement_reviewer_id uuid;
    v_member_id uuid;
    v_member_user_id uuid;
    v_old_role text;
    v_new_member_id uuid;
    v_member_record jsonb;
    v_result jsonb;
    v_public_reply jsonb;
    v_actor jsonb;
    v_target_role_fact text;
    v_lock_ids bigint;
    v_lock_deleted bigint;
    v_annotation_work boolean;
    v_review_work boolean;
    v_audit_id bigint;
    v_rows integer;
BEGIN
    IF NOT aap_workspace_membership_v1._has_exact_keys(
        p_command, ARRAY['schema', 'request', 'transaction_id']::text[]
    ) OR pg_catalog.jsonb_typeof(p_command->'schema') IS DISTINCT FROM 'string'
       OR p_command->>'schema' IS DISTINCT FROM 'aap-workspace-membership-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'transaction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_command->>'transaction_id') = 0
       OR aap_workspace_membership_v1._request_is_valid(p_command->'request') IS DISTINCT FROM true THEN
        RETURN aap_workspace_membership_v1._failure(
            400, pg_catalog.to_jsonb('invalid_request'::text)
        );
    END IF;

    v_request := p_command->'request';
    v_operation := v_request->>'operation';
    IF v_operation = 'preview_role_change' THEN
        RETURN aap_workspace_membership_v1._failure(
            400, pg_catalog.to_jsonb('invalid_request'::text)
        );
    END IF;
    v_operation_id := (v_request->>'operation_id')::uuid;
    v_actor_id := (v_request->>'actor_id')::uuid;
    v_project_id := (v_request->>'project_id')::uuid;
    v_request_hash := pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(v_request::text, 'UTF8')),
        'hex'
    );
    v_transaction_id := p_command->>'transaction_id';
    v_binding_text := NULLIF(
        pg_catalog.current_setting('aap.workspace_membership_prepare', true), ''
    );
    IF v_binding_text IS NULL THEN
        RETURN aap_workspace_membership_v1._failure(
            409, pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;
    BEGIN
        v_binding := v_binding_text::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_workspace_membership_v1._failure(
            409, pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END;
    IF NOT aap_workspace_membership_v1._has_exact_keys(
        v_binding,
        ARRAY['transaction_id', 'operation_id', 'request_hash', 'facts_hash']::text[]
    ) OR pg_catalog.jsonb_typeof(v_binding->'transaction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_binding->'operation_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_binding->'request_hash') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_binding->'facts_hash') IS DISTINCT FROM 'string'
       OR v_binding->>'transaction_id' IS DISTINCT FROM v_transaction_id
       OR v_transaction_id IS DISTINCT FROM pg_catalog.txid_current()::text
       OR v_binding->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_binding->>'request_hash' IS DISTINCT FROM v_request_hash
       OR (v_binding->>'facts_hash') COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_workspace_membership_v1._failure(
            409, pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;
    v_facts_hash := v_binding->>'facts_hash';

    -- Reacquire the exact prepared locks and re-read the typed facts in this
    -- transaction. This makes the native command safe even when called by a
    -- trusted owner adapter that was retried around a savepoint.
    PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
    v_revalidated := aap_workspace_membership_v1.prepare_membership_command(v_request);
    IF pg_catalog.jsonb_typeof(v_revalidated) IS DISTINCT FROM 'object'
       OR v_revalidated->>'schema' IS DISTINCT FROM 'aap-workspace-membership-native/v1'
       OR pg_catalog.jsonb_typeof(v_revalidated->'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace membership revalidation result';
    END IF;
    IF v_revalidated->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
        RETURN v_revalidated;
    END IF;

    v_revalidated_data := v_revalidated->'data';
    IF v_revalidated_data->>'kind' = 'replay' THEN
        IF v_revalidated_data->>'operation_id' IS DISTINCT FROM v_operation_id::text
           OR v_revalidated_data->>'operation' IS DISTINCT FROM v_operation
           OR v_revalidated_data->>'actor_id' IS DISTINCT FROM v_actor_id::text
           OR v_revalidated_data->>'project_id' IS DISTINCT FROM v_project_id::text
           OR pg_catalog.jsonb_typeof(v_revalidated_data #> '{reply,data}') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace membership concurrent replay';
        END IF;
        v_result := v_revalidated_data #> '{reply,data}';
        PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-membership-native/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'kind', 'applied',
                'operation_id', v_operation_id,
                'operation', v_operation,
                'actor_id', v_actor_id,
                'project_id', v_project_id,
                'result', v_result
            )
        );
    END IF;
    IF v_revalidated_data->>'kind' IS DISTINCT FROM 'facts'
       OR v_revalidated_data->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_revalidated_data->>'operation' IS DISTINCT FROM v_operation
       OR v_revalidated_data->>'actor_id' IS DISTINCT FROM v_actor_id::text
       OR v_revalidated_data->>'project_id' IS DISTINCT FROM v_project_id::text
       OR v_revalidated_data->>'transaction_id' IS DISTINCT FROM v_transaction_id THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace membership revalidation facts';
    END IF;
    v_revalidated_binding_text := NULLIF(
        pg_catalog.current_setting('aap.workspace_membership_prepare', true), ''
    );
    IF v_revalidated_binding_text IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Workspace membership revalidation did not bind its facts';
    END IF;
    BEGIN
        v_revalidated_binding := v_revalidated_binding_text::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Workspace membership revalidation binding is invalid';
    END;
    IF NOT aap_workspace_membership_v1._has_exact_keys(
        v_revalidated_binding,
        ARRAY['transaction_id', 'operation_id', 'request_hash', 'facts_hash']::text[]
    ) OR v_revalidated_binding->>'transaction_id' IS DISTINCT FROM v_transaction_id
       OR v_revalidated_binding->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_revalidated_binding->>'request_hash' IS DISTINCT FROM v_request_hash
       OR v_revalidated_binding->>'facts_hash' IS DISTINCT FROM v_facts_hash THEN
        PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
        RETURN aap_workspace_membership_v1._failure(
            409,
            pg_catalog.jsonb_build_object(
                'reason', 'stale_resource_snapshot',
                'detail', '资源已变化，请重新预览'
            )
        );
    END IF;

    v_actor := v_revalidated_data->'actor';
    IF v_operation = 'add_member' THEN
        v_target_user_id := (v_request #>> '{payload,target_user_id}')::uuid;
        SELECT account.role INTO v_target_role_fact
        FROM pg_catalog.jsonb_to_recordset(v_revalidated_data->'accounts') AS account(
            id uuid, role text, is_active boolean
        )
        WHERE account.id = v_target_user_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace membership add target facts are missing';
        END IF;
        INSERT INTO public.project_members (
            id, project_id, user_id, role, assigned_by
        ) VALUES (
            pg_catalog.gen_random_uuid(), v_project_id, v_target_user_id,
            v_request #>> '{payload,project_role}', v_actor_id
        )
        ON CONFLICT (project_id, user_id) DO NOTHING
        RETURNING id INTO v_member_id;
        IF NOT FOUND THEN
            PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
            RETURN aap_workspace_membership_v1._failure(
                409, pg_catalog.to_jsonb('该用户已在项目中'::text)
            );
        END IF;
        v_member_record := aap_workspace_membership_v1._membership_record(
            v_project_id, v_member_id
        );
        IF pg_catalog.jsonb_typeof(v_member_record) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace membership insert result is missing';
        END IF;
        v_result := pg_catalog.jsonb_build_object('member', v_member_record);
        v_audit_id := aap_audit_v1.record_workspace_membership_event(
            'project.member_add',
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id,
                'email', v_actor->>'email',
                'role', v_actor->>'role'
            ),
            v_member_id,
            v_request->'audit_context',
            pg_catalog.jsonb_build_object(
                'project_id', v_project_id,
                'user_id', v_target_user_id,
                'project_role', v_request #>> '{payload,project_role}',
                'platform_role', v_target_role_fact
            )
        );
    ELSIF v_operation = 'change_role' THEN
        v_member_id := (v_request #>> '{payload,member_id}')::uuid;
        v_member_user_id := (v_revalidated_data #>> '{member,user_id}')::uuid;
        v_old_role := v_revalidated_data #>> '{member,role}';
        v_target_role := v_request #>> '{payload,target_role}';
        v_replacement_annotator_id := NULLIF(
            v_request #>> '{payload,replacement_annotator_id}', ''
        )::uuid;
        IF pg_catalog.jsonb_typeof(
            v_request #> '{payload,replacement_annotator_id}'
        ) = 'null' THEN
            v_replacement_annotator_id := NULL;
        END IF;
        v_replacement_reviewer_id := NULLIF(
            v_request #>> '{payload,replacement_reviewer_id}', ''
        )::uuid;
        IF pg_catalog.jsonb_typeof(
            v_request #> '{payload,replacement_reviewer_id}'
        ) = 'null' THEN
            v_replacement_reviewer_id := NULL;
        END IF;
        v_lock_ids := pg_catalog.jsonb_array_length(
            v_revalidated_data #> '{labeling,lock_task_ids}'
        )::bigint;
        v_annotation_work :=
            pg_catalog.jsonb_array_length(
                v_revalidated_data #> '{labeling,annotation_task_ids}'
            ) > 0
            OR pg_catalog.jsonb_array_length(
                v_revalidated_data #> '{labeling,batch_annotator_ids}'
            ) > 0;
        v_review_work :=
            pg_catalog.jsonb_array_length(
                v_revalidated_data #> '{labeling,review_task_ids}'
            ) > 0
            OR pg_catalog.jsonb_array_length(
                v_revalidated_data #> '{labeling,review_claim_task_ids}'
            ) > 0
            OR pg_catalog.jsonb_array_length(
                v_revalidated_data #> '{labeling,batch_reviewer_ids}'
            ) > 0;
        IF v_target_role <> 'annotator'
           AND v_annotation_work
           AND v_replacement_annotator_id IS NOT NULL THEN
            PERFORM aap_labeling_v1.handoff_membership_annotation(
                v_project_id, v_member_user_id, v_replacement_annotator_id
            );
        END IF;
        IF v_target_role <> 'reviewer'
           AND v_review_work
           AND v_replacement_reviewer_id IS NOT NULL THEN
            PERFORM aap_labeling_v1.handoff_membership_reviewer(
                v_project_id, v_member_user_id, v_replacement_reviewer_id
            );
        END IF;
        v_lock_deleted := aap_labeling_v1.release_membership_task_locks(
            v_project_id, v_member_user_id
        );
        IF v_lock_deleted IS NULL OR v_lock_deleted < 0 OR v_lock_ids < 0 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Labeling membership task-lock release result';
        END IF;
        UPDATE public.project_members AS member
        SET role = v_target_role,
            version = member.version + 1,
            updated_at = pg_catalog.clock_timestamp()
        WHERE member.project_id = v_project_id
          AND member.id = v_member_id
        RETURNING member.id INTO v_new_member_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 OR v_new_member_id IS DISTINCT FROM v_member_id THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace membership role update result is invalid';
        END IF;
        v_member_record := aap_workspace_membership_v1._membership_record(
            v_project_id, v_member_id
        );
        IF pg_catalog.jsonb_typeof(v_member_record) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace membership role result is missing';
        END IF;
        v_result := pg_catalog.jsonb_build_object('member', v_member_record);
        v_audit_id := aap_audit_v1.record_workspace_membership_event(
            'project.member_role_change',
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id,
                'email', v_actor->>'email',
                'role', v_actor->>'role'
            ),
            v_member_id,
            v_request->'audit_context',
            pg_catalog.jsonb_build_object(
                'project_id', v_project_id,
                'user_id', v_member_user_id,
                'from_role', v_old_role,
                'to_role', v_target_role,
                'version', v_member_record->'version',
                'reason', v_request #>> '{payload,reason}',
                'replacement_annotator_id', v_replacement_annotator_id,
                'replacement_reviewer_id', v_replacement_reviewer_id,
                'released_lock_count', v_lock_ids
            )
        );
    ELSE
        v_member_id := (v_request #>> '{payload,member_id}')::uuid;
        v_member_user_id := (v_revalidated_data #>> '{member,user_id}')::uuid;
        v_old_role := v_revalidated_data #>> '{member,role}';
        v_lock_ids := pg_catalog.jsonb_array_length(
            v_revalidated_data #> '{labeling,lock_task_ids}'
        )::bigint;
        v_lock_deleted := aap_labeling_v1.release_membership_task_locks(
            v_project_id, v_member_user_id
        );
        IF v_lock_deleted IS NULL OR v_lock_deleted < 0 OR v_lock_ids < 0 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Labeling membership task-lock release result';
        END IF;
        v_audit_id := aap_audit_v1.record_workspace_membership_event(
            'project.member_remove',
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id,
                'email', v_actor->>'email',
                'role', v_actor->>'role'
            ),
            v_member_id,
            v_request->'audit_context',
            pg_catalog.jsonb_build_object(
                'project_id', v_project_id,
                'user_id', v_member_user_id,
                'project_role', v_old_role
            )
        );
        DELETE FROM public.project_members AS member
        WHERE member.project_id = v_project_id
          AND member.id = v_member_id
        RETURNING member.id INTO v_new_member_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 OR v_new_member_id IS DISTINCT FROM v_member_id THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace membership delete result is invalid';
        END IF;
        v_result := pg_catalog.jsonb_build_object(
            'member_id', v_member_id,
            'removed', true
        );
    END IF;

    IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Workspace membership Audit result is invalid';
    END IF;
    v_public_reply := pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-membership/v1',
        'ok', true,
        'operation_id', v_operation_id,
        'operation', v_operation,
        'actor_id', v_actor_id,
        'project_id', v_project_id,
        'data', v_result
    );
    INSERT INTO public.workspace_membership_operation_receipts (
        operation_id, actor_id, project_id, operation, request_hash,
        result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_project_id, v_operation,
        v_request_hash, v_public_reply, v_audit_id
    );
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Workspace membership receipt was not recorded';
    END IF;

    PERFORM pg_catalog.set_config('aap.workspace_membership_prepare', '', true);
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-membership-native/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'kind', 'applied',
            'operation_id', v_operation_id,
            'operation', v_operation,
            'actor_id', v_actor_id,
            'project_id', v_project_id,
            'result', v_result
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_membership_v1.apply_membership_command(jsonb)
    FROM PUBLIC;
