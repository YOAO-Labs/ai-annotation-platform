-- Native Workspace owner transfer; the API caller retains transaction ownership.
CREATE SCHEMA aap_workspace_owner_transfer_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_owner_transfer_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.workspace_owner_transfer_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    new_owner_id uuid NOT NULL,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    result jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
COMMENT ON TABLE public.workspace_owner_transfer_operation_receipts IS
    'Immutable private Workspace owner-transfer success replies; no foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE public.workspace_owner_transfer_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_owner_transfer_v1._failure(
    p_status_code integer,
    p_detail jsonb,
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
        'schema', 'aap-workspace-owner-transfer/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'status_code', p_status_code,
            'detail', p_detail,
            'rollback', p_rollback
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_owner_transfer_v1._failure(
    integer, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_owner_transfer_v1.owner_transfer_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-owner-transfer/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_owner_transfer_v1.owner_transfer_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_owner_transfer_v1.transfer_project_owner(
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
    IF NOT aap_workspace_membership_v1._has_exact_keys(
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
        RETURN aap_workspace_owner_transfer_v1._failure(
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
        RETURN aap_workspace_owner_transfer_v1._failure(
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

    IF NOT aap_workspace_membership_v1._has_exact_keys(
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
           OR NOT aap_workspace_membership_v1._has_exact_keys(
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
        RETURN aap_workspace_owner_transfer_v1._failure(
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
            RETURN aap_workspace_owner_transfer_v1._failure(
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
        RETURN aap_workspace_owner_transfer_v1._failure(
            404, pg_catalog.to_jsonb('项目不存在'::text)
        );
    END IF;

    IF v_has_receipt THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.project_id IS DISTINCT FROM v_project_id
           OR v_receipt.new_owner_id IS DISTINCT FROM v_new_owner_id
           OR v_receipt.request_hash IS DISTINCT FROM v_request_hash THEN
            RETURN aap_workspace_owner_transfer_v1._failure(
                409,
                pg_catalog.jsonb_build_object('reason', 'operation_conflict')
            );
        END IF;

        v_result := v_receipt.result;
        IF NOT aap_workspace_membership_v1._has_exact_keys(
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
        IF NOT aap_workspace_membership_v1._has_exact_keys(
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
        RETURN aap_workspace_owner_transfer_v1._failure(
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
REVOKE ALL ON FUNCTION aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    FROM PUBLIC;
