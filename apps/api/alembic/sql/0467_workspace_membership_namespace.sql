-- Move Workspace membership ownership without changing wire or receipt identities.
-- Legacy admission must be stopped and its callers drained before this transaction.
DO $cutover$
DECLARE
    v_namespace oid := pg_catalog.to_regnamespace('aap_workspace_membership_v1');
    v_transfer oid;
    v_functions text[];
    v_dependents text[];
    v_legacy oid;
BEGIN
    IF v_namespace IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42704',
            MESSAGE = 'Workspace membership source namespace is missing';
    END IF;
    IF pg_catalog.to_regnamespace('yoao_workspace_membership_v1') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42P06',
            MESSAGE = 'Workspace membership destination namespace already exists';
    END IF;
    SELECT pg_catalog.array_agg(
        function.proname || '(' || pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY function.proname COLLATE "C"
    ) INTO v_functions
    FROM pg_catalog.pg_proc AS function
    WHERE function.pronamespace = v_namespace;
    IF v_functions IS DISTINCT FROM ARRAY[
        '_failure(integer, jsonb, jsonb, boolean)',
        '_has_exact_keys(jsonb, text[])',
        '_membership_record(uuid, uuid)',
        '_request_is_valid(jsonb)',
        'apply_membership_command(jsonb)',
        'membership_policy()',
        'prepare_membership_command(jsonb)'
    ]::text[] OR EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace = v_namespace
          AND (function.prokind <> 'f' OR NOT function.prosecdef)
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_class WHERE relnamespace = v_namespace
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_type WHERE typnamespace = v_namespace
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace membership source namespace is not the owned function surface';
    END IF;
    SELECT pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname COLLATE "C", function.proname COLLATE "C"
    ) INTO v_dependents
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE function.pronamespace <> v_namespace
      AND function.prosrc LIKE '%aap_workspace_membership_v1.%';
    IF v_dependents IS DISTINCT FROM ARRAY[
        'yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)'
    ]::text[] THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace membership namespace has unexpected function dependents',
            DETAIL = COALESCE(
                pg_catalog.array_to_string(v_dependents, ', '), 'none'
            );
    END IF;
    v_transfer := pg_catalog.to_regprocedure(
        'yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)'
    );
    IF v_transfer IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42704',
            MESSAGE = 'Workspace owner-transfer function is missing';
    END IF;
    SELECT oid INTO v_legacy FROM pg_catalog.pg_roles
    WHERE rolname = 'workspace_membership_client';
    IF EXISTS (
        SELECT FROM pg_catalog.pg_roles WHERE oid = v_legacy AND rolcanlogin
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'Disable legacy Workspace membership admission before namespace cutover';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_roles
        WHERE oid = v_legacy
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_auth_members
        WHERE member = v_legacy OR roleid = v_legacy
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Legacy Workspace membership role is not a closed client';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_stat_activity AS activity
        WHERE activity.datid = (
            SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()
        )
          AND activity.pid <> pg_catalog.pg_backend_pid()
          AND COALESCE(activity.backend_type, 'client backend') = 'client backend'
          AND activity.usesysid IS NOT NULL
          AND pg_catalog.has_schema_privilege(activity.usesysid, v_namespace, 'USAGE')
          AND EXISTS (
              SELECT 1 FROM pg_catalog.pg_proc AS function
              WHERE function.pronamespace = v_namespace
                AND function.proname IN (
                    'membership_policy',
                    'prepare_membership_command',
                    'apply_membership_command'
                )
                AND pg_catalog.has_function_privilege(
                    activity.usesysid, function.oid, 'EXECUTE'
                )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'Drain Workspace membership callers before namespace cutover';
    END IF;
END;
$cutover$;
-- aap-statement-break
ALTER SCHEMA aap_workspace_membership_v1 RENAME TO yoao_workspace_membership_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_workspace_membership_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_workspace_membership_v1 FROM PUBLIC;
-- aap-statement-break
DO $retire$
BEGIN
    IF EXISTS (
        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'workspace_membership_client'
    ) THEN
        REVOKE ALL ON SCHEMA yoao_workspace_membership_v1
            FROM workspace_membership_client;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_workspace_membership_v1
            FROM workspace_membership_client;
    END IF;
END;
$retire$;
-- aap-statement-break
-- Native Workspace-owned project membership commands. Policy and canonical
-- resource snapshots remain in the portable Workspace manager.
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1._has_exact_keys(
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
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1._failure(
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
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1._failure(
    integer, jsonb, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1._request_is_valid(
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
    IF NOT yoao_workspace_membership_v1._has_exact_keys(
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
    IF NOT yoao_workspace_membership_v1._has_exact_keys(
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
        RETURN yoao_workspace_membership_v1._has_exact_keys(
                   v_payload,
                   ARRAY['target_user_id', 'project_role']::text[]
               )
           AND pg_catalog.jsonb_typeof(v_payload->'target_user_id') = 'string'
           AND (v_payload->>'target_user_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND pg_catalog.jsonb_typeof(v_payload->'project_role') = 'string';
    ELSIF v_operation = 'preview_role_change' THEN
        RETURN yoao_workspace_membership_v1._has_exact_keys(
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
        RETURN yoao_workspace_membership_v1._has_exact_keys(
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
           AND pg_catalog.json_typeof((v_payload->>'preview_token')::json) = 'string'
           AND pg_catalog.jsonb_typeof(v_payload->'reason') = 'string'
           AND pg_catalog.json_typeof((v_payload->>'reason')::json) = 'string';
    ELSE
        RETURN yoao_workspace_membership_v1._has_exact_keys(
                   v_payload,
                   ARRAY['member_id']::text[]
               )
           AND pg_catalog.jsonb_typeof(v_payload->'member_id') = 'string'
           AND (v_payload->>'member_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    END IF;
EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1._request_is_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1._membership_record(
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
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1._membership_record(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1.membership_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-membership-native/v1',
        'opaque_text_codec', 'json-string-ascii/v1',
        'operations', ARRAY[
            'add_member', 'preview_role_change', 'change_role', 'remove_member'
        ]::text[]
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1.membership_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1.prepare_membership_command(
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
    IF yoao_workspace_membership_v1._request_is_valid(p_request) IS DISTINCT FROM true THEN
        RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
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
        PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
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
            RETURN yoao_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
        SELECT project.id, project.owner_id
        INTO v_project
        FROM public.projects AS project
        WHERE project.id = v_project_id;
        IF NOT FOUND THEN
            RETURN yoao_workspace_membership_v1._failure(
                404, pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;
        IF v_actor.role <> 'super_admin'
           AND NOT (v_actor.role = 'project_admin'
                    AND v_project.owner_id = v_actor_id) THEN
            RETURN yoao_workspace_membership_v1._failure(
                403,
                pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行成员变更'::text)
            );
        END IF;
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.project_id IS DISTINCT FROM v_project_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.request_hash IS DISTINCT FROM v_request_hash THEN
            RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
        IF v_actor.role <> 'super_admin'
           AND NOT (v_actor.role = 'project_admin'
                    AND v_project.owner_id = v_actor_id) THEN
            RETURN yoao_workspace_membership_v1._failure(
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
                RETURN yoao_workspace_membership_v1._failure(
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
                RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
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
            RETURN yoao_workspace_membership_v1._failure(
                401, pg_catalog.to_jsonb('账号已停用'::text)
            );
        END IF;
        IF v_locked_actor.role <> 'super_admin'
           AND NOT (v_locked_actor.role = 'project_admin'
                    AND v_project.owner_id = v_actor_id) THEN
            RETURN yoao_workspace_membership_v1._failure(
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
                    RETURN yoao_workspace_membership_v1._failure(
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
                RETURN yoao_workspace_membership_v1._failure(
                    404, pg_catalog.to_jsonb('成员不存在'::text)
                );
            END IF;
        END IF;

        IF v_operation = 'change_role' THEN
            IF v_expected_version IS DISTINCT FROM
               (v_payload->>'expected_version')::numeric THEN
                RETURN yoao_workspace_membership_v1._failure(
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
                RETURN yoao_workspace_membership_v1._failure(
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
                RETURN yoao_workspace_membership_v1._failure(
                    404, pg_catalog.to_jsonb('目标用户不存在'::text)
                );
            END IF;
            IF NOT (
                (v_locked_target.role = 'viewer' AND v_project_role = 'viewer')
                OR (v_locked_target.role = 'employee'
                    AND v_project_role IN ('annotator', 'reviewer', 'viewer'))
            ) THEN
                RETURN yoao_workspace_membership_v1._failure(
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
                RETURN yoao_workspace_membership_v1._failure(
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
                    RETURN yoao_workspace_membership_v1._failure(
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
        RETURN yoao_workspace_membership_v1._failure(
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
        RETURN yoao_workspace_membership_v1._failure(
            401, pg_catalog.to_jsonb('账号已停用'::text)
        );
    END IF;
    IF v_actor.role <> 'super_admin'
       AND NOT (v_actor.role = 'project_admin' AND v_project.owner_id = v_actor_id) THEN
        RETURN yoao_workspace_membership_v1._failure(
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
        v_member_json := yoao_workspace_membership_v1._membership_record(
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
                IF NOT yoao_workspace_membership_v1._has_exact_keys(
                    v_label_lock, ARRAY['ok', 'error']::text[]
                ) OR v_label_lock->'ok' IS DISTINCT FROM 'false'::jsonb
                  OR NOT yoao_workspace_membership_v1._has_exact_keys(
                      v_label_lock->'error', ARRAY['reason', 'detail']::text[]
                  ) OR v_label_lock #>> '{error,reason}' IS DISTINCT FROM 'resource_busy'
                  OR pg_catalog.jsonb_typeof(v_label_lock #> '{error,detail}') IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'invalid Labeling membership lock result';
                END IF;
                RETURN yoao_workspace_membership_v1._failure(
                    409,
                    pg_catalog.jsonb_build_object(
                        'reason', 'resource_busy',
                        'detail', v_label_lock #> '{error,detail}'
                    ),
                    NULL,
                    true
                );
            END IF;
            IF NOT yoao_workspace_membership_v1._has_exact_keys(
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
            RETURN yoao_workspace_membership_v1._failure(
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
            'yoao.workspace_membership_prepare',
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
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1.prepare_membership_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_membership_v1.apply_membership_command(
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
    IF NOT yoao_workspace_membership_v1._has_exact_keys(
        p_command, ARRAY['schema', 'request', 'transaction_id']::text[]
    ) OR pg_catalog.jsonb_typeof(p_command->'schema') IS DISTINCT FROM 'string'
       OR p_command->>'schema' IS DISTINCT FROM 'aap-workspace-membership-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'transaction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_command->>'transaction_id') = 0
       OR yoao_workspace_membership_v1._request_is_valid(p_command->'request') IS DISTINCT FROM true THEN
        RETURN yoao_workspace_membership_v1._failure(
            400, pg_catalog.to_jsonb('invalid_request'::text)
        );
    END IF;

    v_request := p_command->'request';
    v_operation := v_request->>'operation';
    IF v_operation = 'preview_role_change' THEN
        RETURN yoao_workspace_membership_v1._failure(
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
        pg_catalog.current_setting('yoao.workspace_membership_prepare', true), ''
    );
    IF v_binding_text IS NULL THEN
        RETURN yoao_workspace_membership_v1._failure(
            409, pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;
    BEGIN
        v_binding := v_binding_text::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN yoao_workspace_membership_v1._failure(
            409, pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END;
    IF NOT yoao_workspace_membership_v1._has_exact_keys(
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
        RETURN yoao_workspace_membership_v1._failure(
            409, pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;
    v_facts_hash := v_binding->>'facts_hash';

    -- Reacquire the exact prepared locks and re-read the typed facts in this
    -- transaction. This makes the native command safe even when called by a
    -- trusted owner adapter that was retried around a savepoint.
    PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
    v_revalidated := yoao_workspace_membership_v1.prepare_membership_command(v_request);
    IF pg_catalog.jsonb_typeof(v_revalidated) IS DISTINCT FROM 'object'
       OR v_revalidated->>'schema' IS DISTINCT FROM 'aap-workspace-membership-native/v1'
       OR pg_catalog.jsonb_typeof(v_revalidated->'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace membership revalidation result';
    END IF;
    IF v_revalidated->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
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
        PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
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
        pg_catalog.current_setting('yoao.workspace_membership_prepare', true), ''
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
    IF NOT yoao_workspace_membership_v1._has_exact_keys(
        v_revalidated_binding,
        ARRAY['transaction_id', 'operation_id', 'request_hash', 'facts_hash']::text[]
    ) OR v_revalidated_binding->>'transaction_id' IS DISTINCT FROM v_transaction_id
       OR v_revalidated_binding->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_revalidated_binding->>'request_hash' IS DISTINCT FROM v_request_hash
       OR v_revalidated_binding->>'facts_hash' IS DISTINCT FROM v_facts_hash THEN
        PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
        RETURN yoao_workspace_membership_v1._failure(
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
            PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
            RETURN yoao_workspace_membership_v1._failure(
                409, pg_catalog.to_jsonb('该用户已在项目中'::text)
            );
        END IF;
        v_member_record := yoao_workspace_membership_v1._membership_record(
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
        v_member_record := yoao_workspace_membership_v1._membership_record(
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
                'reason', (v_request #>> '{payload,reason}')::jsonb,
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

    PERFORM pg_catalog.set_config('yoao.workspace_membership_prepare', '', true);
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
REVOKE ALL ON FUNCTION yoao_workspace_membership_v1.apply_membership_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_workspace_owner_transfer_v1.transfer_project_owner(
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
    v_operation_id uuid;
    v_actor_id uuid;
    v_project_id uuid;
    v_new_owner_id uuid;
    v_request_hash text;
    v_account_ids uuid[];
    v_account_result jsonb;
    v_actor record;
    v_target record;
    v_project record;
    v_receipt record;
    v_has_receipt boolean;
    v_result jsonb;
    v_result_data jsonb;
    v_previous_owner_id uuid;
    v_updated_at timestamptz;
    v_changed boolean;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_original_hint text;
    v_schema_name text;
    v_table_name text;
    v_column_name text;
    v_constraint_name text;
    v_datatype_name text;
    v_account_row_count integer;
    v_unique_account_count integer;
    v_rows integer;
BEGIN
    IF NOT yoao_workspace_membership_v1._has_exact_keys(
        p_request,
        ARRAY['schema', 'operation_id', 'actor_id', 'project_id', 'new_owner_id']::text[]
    )
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-owner-transfer/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
       OR (p_request->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'project_id') IS DISTINCT FROM 'string'
       OR (p_request->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'new_owner_id') IS DISTINCT FROM 'string'
       OR (p_request->>'new_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN yoao_workspace_owner_transfer_v1._failure(
            400, pg_catalog.to_jsonb('invalid_request'::text)
        );
    END IF;

    v_operation_id := (p_request->>'operation_id')::uuid;
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_project_id := (p_request->>'project_id')::uuid;
    v_new_owner_id := (p_request->>'new_owner_id')::uuid;
    v_request_hash := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                'aap-workspace-owner-transfer/v1|' || v_operation_id::text || '|' ||
                v_actor_id::text || '|' || v_project_id::text || '|' ||
                v_new_owner_id::text,
                'UTF8'
            )
        ),
        'hex'
    );

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-workspace-owner-transfer/v1:' || v_operation_id::text, 0
        )
    ) THEN
        RETURN yoao_workspace_owner_transfer_v1._failure(
            409,
            pg_catalog.jsonb_build_object('reason', 'operation_busy')
        );
    END IF;

    SELECT receipt.operation_id, receipt.actor_id, receipt.project_id,
           receipt.new_owner_id, receipt.request_hash, receipt.result
    INTO v_receipt
    FROM public.workspace_owner_transfer_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_has_receipt := FOUND;

    IF v_has_receipt THEN
        v_account_ids := ARRAY[v_actor_id]::uuid[];
    ELSE
        SELECT COALESCE(
            pg_catalog.array_agg(DISTINCT item.id ORDER BY item.id),
            ARRAY[]::uuid[]
        ) INTO v_account_ids
        FROM pg_catalog.unnest(ARRAY[v_actor_id, v_new_owner_id]::uuid[]) AS item(id);
    END IF;

    BEGIN
        v_account_result := aap_identity_v1.lock_accounts_update(
            v_account_ids, true
        );
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS
            v_sqlstate = RETURNED_SQLSTATE,
            v_message = MESSAGE_TEXT,
            v_detail = PG_EXCEPTION_DETAIL,
            v_original_hint = PG_EXCEPTION_HINT,
            v_schema_name = SCHEMA_NAME,
            v_table_name = TABLE_NAME,
            v_column_name = COLUMN_NAME,
            v_constraint_name = CONSTRAINT_NAME,
            v_datatype_name = PG_DATATYPE_NAME;
        RAISE EXCEPTION USING
            ERRCODE = v_sqlstate,
            MESSAGE = v_message,
            DETAIL = v_detail,
            HINT = 'aap.workspace.owner_transfer.account_lock' ||
                CASE WHEN v_original_hint IS NULL OR v_original_hint = ''
                    THEN '' ELSE E'\n' || v_original_hint END,
            SCHEMA = v_schema_name,
            TABLE = v_table_name,
            COLUMN = v_column_name,
            CONSTRAINT = v_constraint_name,
            DATATYPE = v_datatype_name;
    END;

    IF NOT yoao_workspace_membership_v1._has_exact_keys(
        v_account_result, ARRAY['schema', 'accounts']::text[]
    )
       OR v_account_result->>'schema' IS DISTINCT FROM 'aap-identity-account-locks/v1'
       OR pg_catalog.jsonb_typeof(v_account_result->'accounts') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace owner-transfer account lock result';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_account_result->'accounts') AS item(value)
        WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
           OR NOT yoao_workspace_membership_v1._has_exact_keys(
               item.value,
               ARRAY['id', 'name', 'email', 'role', 'is_active']::text[]
           )
           OR pg_catalog.jsonb_typeof(item.value->'id') IS DISTINCT FROM 'string'
           OR (item.value->>'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(item.value->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item.value->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item.value->'role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item.value->'is_active') IS DISTINCT FROM 'boolean'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace owner-transfer account lock row';
    END IF;

    SELECT pg_catalog.count(*)::integer,
           pg_catalog.count(DISTINCT account.id)::integer
    INTO v_account_row_count, v_unique_account_count
    FROM pg_catalog.jsonb_to_recordset(v_account_result->'accounts') AS account(
        id uuid, name text, email text, role text, is_active boolean
    );
    IF v_account_row_count <> v_unique_account_count OR EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_to_recordset(v_account_result->'accounts') AS account(
            id uuid, name text, email text, role text, is_active boolean
        )
        WHERE NOT (account.id = ANY(v_account_ids))
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace owner-transfer account lock set';
    END IF;

    SELECT account.id, account.role, account.is_active
    INTO v_actor
    FROM pg_catalog.jsonb_to_recordset(v_account_result->'accounts') AS account(
        id uuid, role text, is_active boolean
    )
    WHERE account.id = v_actor_id;
    IF NOT FOUND OR v_actor.is_active IS NOT TRUE
       OR v_actor.role IS DISTINCT FROM 'super_admin' THEN
        RETURN yoao_workspace_owner_transfer_v1._failure(
            403, pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;

    IF NOT v_has_receipt THEN
        SELECT account.id, account.role, account.is_active
        INTO v_target
        FROM pg_catalog.jsonb_to_recordset(v_account_result->'accounts') AS account(
            id uuid, role text, is_active boolean
        )
        WHERE account.id = v_new_owner_id;
        IF NOT FOUND OR v_target.is_active IS NOT TRUE THEN
            RETURN yoao_workspace_owner_transfer_v1._failure(
                404, pg_catalog.to_jsonb('目标用户不存在'::text)
            );
        END IF;
    END IF;

    BEGIN
        SELECT project.id, project.owner_id, project.updated_at
        INTO v_project
        FROM public.projects AS project
        WHERE project.id = v_project_id
        FOR UPDATE OF project NOWAIT;
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS
            v_sqlstate = RETURNED_SQLSTATE,
            v_message = MESSAGE_TEXT,
            v_detail = PG_EXCEPTION_DETAIL,
            v_original_hint = PG_EXCEPTION_HINT,
            v_schema_name = SCHEMA_NAME,
            v_table_name = TABLE_NAME,
            v_column_name = COLUMN_NAME,
            v_constraint_name = CONSTRAINT_NAME,
            v_datatype_name = PG_DATATYPE_NAME;
        IF v_sqlstate IN ('55P03', '40001', '40P01') THEN
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.owner_transfer.project_lock' ||
                    CASE WHEN v_original_hint IS NULL OR v_original_hint = ''
                        THEN '' ELSE E'\n' || v_original_hint END,
                SCHEMA = v_schema_name,
                TABLE = v_table_name,
                COLUMN = v_column_name,
                CONSTRAINT = v_constraint_name,
                DATATYPE = v_datatype_name;
        END IF;
        RAISE;
    END;
    IF NOT FOUND THEN
        RETURN yoao_workspace_owner_transfer_v1._failure(
            404, pg_catalog.to_jsonb('项目不存在'::text)
        );
    END IF;

    IF v_has_receipt THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.project_id IS DISTINCT FROM v_project_id
           OR v_receipt.new_owner_id IS DISTINCT FROM v_new_owner_id
           OR v_receipt.request_hash IS DISTINCT FROM v_request_hash THEN
            RETURN yoao_workspace_owner_transfer_v1._failure(
                409,
                pg_catalog.jsonb_build_object('reason', 'operation_conflict')
            );
        END IF;

        v_result := v_receipt.result;
        IF NOT yoao_workspace_membership_v1._has_exact_keys(
            v_result,
            ARRAY[
                'schema', 'ok', 'operation_id', 'actor_id', 'project_id',
                'new_owner_id', 'data'
            ]::text[]
        )
           OR v_result->>'schema' IS DISTINCT FROM 'aap-workspace-owner-transfer/v1'
           OR v_result->'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_result->>'operation_id' IS DISTINCT FROM v_operation_id::text
           OR v_result->>'actor_id' IS DISTINCT FROM v_actor_id::text
           OR v_result->>'project_id' IS DISTINCT FROM v_project_id::text
           OR v_result->>'new_owner_id' IS DISTINCT FROM v_new_owner_id::text THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer success receipt';
        END IF;
        v_result_data := v_result->'data';
        IF NOT yoao_workspace_membership_v1._has_exact_keys(
            v_result_data,
            ARRAY['previous_owner_id', 'updated_at', 'changed']::text[]
        )
           OR pg_catalog.jsonb_typeof(v_result_data->'previous_owner_id') NOT IN (
               'null', 'string'
           )
           OR (pg_catalog.jsonb_typeof(v_result_data->'previous_owner_id') = 'string'
               AND (v_result_data->>'previous_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(v_result_data->'updated_at') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_result_data->'changed') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer result data';
        END IF;
        IF pg_catalog.jsonb_typeof(v_result_data->'previous_owner_id') = 'string' THEN
            v_previous_owner_id := (v_result_data->>'previous_owner_id')::uuid;
        ELSE
            v_previous_owner_id := NULL;
        END IF;
        BEGIN
            v_updated_at := (v_result_data->>'updated_at')::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer receipt timestamp';
        END;
        IF NOT pg_catalog.isfinite(v_updated_at) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer receipt timestamp';
        END IF;
        v_changed := (v_result_data->>'changed')::boolean;
        IF v_changed IS DISTINCT FROM
           (v_previous_owner_id IS DISTINCT FROM v_new_owner_id) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer receipt change flag';
        END IF;
        RETURN v_result;
    END IF;

    IF v_target.role IS DISTINCT FROM 'project_admin' THEN
        RETURN yoao_workspace_owner_transfer_v1._failure(
            400, pg_catalog.to_jsonb('仅可转移给 project_admin'::text)
        );
    END IF;

    v_previous_owner_id := v_project.owner_id;
    v_changed := v_previous_owner_id IS DISTINCT FROM v_new_owner_id;
    IF v_changed THEN
        UPDATE public.projects AS project
        SET owner_id = v_new_owner_id,
            updated_at = pg_catalog.now()
        WHERE project.id = v_project_id
        RETURNING project.updated_at INTO v_updated_at;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 OR v_updated_at IS NULL
           OR NOT pg_catalog.isfinite(v_updated_at) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer update result';
        END IF;
    ELSE
        v_updated_at := v_project.updated_at;
        IF v_updated_at IS NULL OR NOT pg_catalog.isfinite(v_updated_at) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace owner-transfer project timestamp';
        END IF;
    END IF;

    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-owner-transfer/v1',
        'ok', true,
        'operation_id', v_operation_id,
        'actor_id', v_actor_id,
        'project_id', v_project_id,
        'new_owner_id', v_new_owner_id,
        'data', pg_catalog.jsonb_build_object(
            'previous_owner_id', v_previous_owner_id,
            'updated_at', v_updated_at,
            'changed', v_changed
        )
    );
    IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
       OR v_result->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_result->>'actor_id' IS DISTINCT FROM v_actor_id::text
       OR v_result->>'project_id' IS DISTINCT FROM v_project_id::text
       OR v_result->>'new_owner_id' IS DISTINCT FROM v_new_owner_id::text
       OR (v_result #>> '{data,changed}')::boolean IS DISTINCT FROM v_changed THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Workspace owner-transfer success result';
    END IF;

    INSERT INTO public.workspace_owner_transfer_operation_receipts (
        operation_id, actor_id, project_id, new_owner_id, request_hash, result
    ) VALUES (
        v_operation_id, v_actor_id, v_project_id, v_new_owner_id,
        v_request_hash, v_result
    );
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Workspace owner-transfer receipt was not recorded';
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    FROM PUBLIC;
-- aap-statement-break
DO $verify$
DECLARE
    v_functions text[];
BEGIN
    IF pg_catalog.to_regnamespace('aap_workspace_membership_v1') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace membership legacy namespace still exists';
    END IF;
    SELECT pg_catalog.array_agg(
        function.proname || '(' || pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY function.proname COLLATE "C"
    ) INTO v_functions
    FROM pg_catalog.pg_proc AS function
    WHERE function.pronamespace =
          pg_catalog.to_regnamespace('yoao_workspace_membership_v1');
    IF v_functions IS DISTINCT FROM ARRAY[
        '_failure(integer, jsonb, jsonb, boolean)',
        '_has_exact_keys(jsonb, text[])',
        '_membership_record(uuid, uuid)',
        '_request_is_valid(jsonb)',
        'apply_membership_command(jsonb)',
        'membership_policy()',
        'prepare_membership_command(jsonb)'
    ]::text[] OR EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace =
              pg_catalog.to_regnamespace('yoao_workspace_membership_v1')
          AND (function.prokind <> 'f' OR NOT function.prosecdef)
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_class
        WHERE relnamespace = pg_catalog.to_regnamespace('yoao_workspace_membership_v1')
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_type
        WHERE typnamespace = pg_catalog.to_regnamespace('yoao_workspace_membership_v1')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace membership destination namespace is not the owned function surface';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        WHERE function.prosrc LIKE '%aap_workspace_membership_v1.%'
           OR function.prosrc LIKE '%aap.workspace_membership_prepare%'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace membership namespace still has legacy references';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = 'workspace_membership_client'
          AND (
              pg_catalog.has_schema_privilege(
                  role.oid, 'yoao_workspace_membership_v1', 'USAGE'
              )
              OR EXISTS (
                  SELECT 1 FROM pg_catalog.pg_proc AS function
                  WHERE function.pronamespace =
                        pg_catalog.to_regnamespace('yoao_workspace_membership_v1')
                    AND pg_catalog.has_function_privilege(
                        role.oid, function.oid, 'EXECUTE'
                    )
              )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Legacy Workspace membership role retains capabilities';
    END IF;
END;
$verify$;
