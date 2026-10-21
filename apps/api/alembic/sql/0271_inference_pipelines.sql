-- Inference-owned fixed data plane for named pipeline reads and writes.
CREATE SCHEMA aap_inference_pipelines_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_pipelines_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_pipelines_v1.operation_states (
    transaction_id bigint NOT NULL,
    operation_id uuid NOT NULL,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    request jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(request) = 'object'),
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation IN (
        'create_pipeline', 'update_pipeline', 'delete_pipeline', 'apply_pipeline'
    )),
    target_id uuid,
    completed_actions text[] NOT NULL DEFAULT ARRAY[]::text[],
    selected_pipeline_id uuid,
    selected_project_id uuid,
    selected_row_exists boolean NOT NULL DEFAULT false,
    source_stages jsonb,
    source_pipeline_id uuid,
    last_backend_id uuid,
    last_backend_found boolean NOT NULL DEFAULT false,
    first_pool_id uuid,
    visibility_project_id uuid,
    visibility_request_id uuid,
    visibility_get_count smallint NOT NULL DEFAULT 0,
    visibility_actor_seen boolean NOT NULL DEFAULT false,
    visibility_actor_active boolean NOT NULL DEFAULT false,
    visibility_project_exists boolean NOT NULL DEFAULT false,
    visibility_member_seen boolean NOT NULL DEFAULT false,
    access_phases text[] NOT NULL DEFAULT ARRAY[]::text[],
    access_actor_active boolean NOT NULL DEFAULT false,
    access_project_exists boolean NOT NULL DEFAULT false,
    default_cleared_project_id uuid,
    default_clear_seen boolean NOT NULL DEFAULT false,
    source_usage_seen boolean NOT NULL DEFAULT false,
    update_seen boolean NOT NULL DEFAULT false,
    delete_seen boolean NOT NULL DEFAULT false,
    resource_id uuid,
    PRIMARY KEY (transaction_id, operation_id)
);
-- aap-statement-break
COMMENT ON TABLE aap_inference_pipelines_v1.operation_states IS
    'Private transaction-local phase binding for one Inference pipeline command; contains no foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE aap_inference_pipelines_v1.operation_states FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_pipelines_v1.operation_attempts (
    transaction_id bigint NOT NULL,
    operation_id uuid NOT NULL,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    PRIMARY KEY (transaction_id, operation_id)
);
-- aap-statement-break
COMMENT ON TABLE aap_inference_pipelines_v1.operation_attempts IS
    'Private deferred commit guard for pipeline writes; contains no foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE aap_inference_pipelines_v1.operation_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_pipelines_v1.operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation IN (
        'create_pipeline', 'update_pipeline', 'delete_pipeline', 'apply_pipeline'
    )),
    target_id uuid,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    request jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(request) = 'object'),
    resource_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
COMMENT ON TABLE aap_inference_pipelines_v1.operation_receipts IS
    'Immutable accepted pipeline command receipts; no foreign keys so replay survives resource deletion.';
-- aap-statement-break
REVOKE ALL ON TABLE aap_inference_pipelines_v1.operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._has_exact_keys(
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
        ELSE (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_value))
                 = pg_catalog.cardinality(p_keys)
             AND p_value ?& p_keys
             AND NOT EXISTS (
                 SELECT 1
                 FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
                 WHERE NOT (field.name = ANY (p_keys))
             )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._has_exact_keys(jsonb, text[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._actor_is_valid(p_actor jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_inference_pipelines_v1._has_exact_keys(
               p_actor, ARRAY['id', 'role']::text[]
           )
       AND pg_catalog.jsonb_typeof(p_actor->'id') = 'string'
       AND (p_actor->>'id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       AND pg_catalog.jsonb_typeof(p_actor->'role') = 'string'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._actor_is_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._command_is_valid(p_request jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN NOT aap_inference_pipelines_v1._has_exact_keys(
            p_request,
            ARRAY[
                'schema', 'operation_id', 'operation', 'admitted_actor',
                'target_id', 'payload_ascii'
            ]::text[]
        ) THEN false
        WHEN pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
          OR p_request->>'schema' IS DISTINCT FROM 'aap-inference-pipelines/v1'
          OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
          OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
          OR p_request->>'operation' NOT IN (
              'create_pipeline', 'update_pipeline', 'delete_pipeline', 'apply_pipeline'
          )
          OR NOT aap_inference_pipelines_v1._actor_is_valid(p_request->'admitted_actor')
          OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
          OR (
              pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
              AND (p_request->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          )
          OR pg_catalog.jsonb_typeof(p_request->'payload_ascii') IS DISTINCT FROM 'string'
          OR pg_catalog.char_length(p_request->>'payload_ascii') > 16777216
          OR pg_catalog.octet_length(pg_catalog.convert_to(
                 p_request->>'payload_ascii', 'UTF8'
             )) <> pg_catalog.char_length(p_request->>'payload_ascii')
          OR (
              p_request->>'operation' = 'create_pipeline'
              AND pg_catalog.jsonb_typeof(p_request->'target_id') IS DISTINCT FROM 'null'
          )
          OR (
              p_request->>'operation' <> 'create_pipeline'
              AND pg_catalog.jsonb_typeof(p_request->'target_id') IS DISTINCT FROM 'string'
          ) THEN false
        ELSE true
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._command_is_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._query_is_valid(p_request jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN NOT aap_inference_pipelines_v1._has_exact_keys(
            p_request,
            ARRAY[
                'schema', 'request_id', 'operation', 'admitted_actor',
                'target_id', 'filters', 'evidence'
            ]::text[]
        ) THEN false
        WHEN pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
          OR p_request->>'schema' IS DISTINCT FROM 'aap-inference-pipelines/v1'
          OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'string'
          OR (p_request->>'request_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
          OR p_request->>'operation' NOT IN (
              'list_pipelines', 'get_pipeline', 'get_project_projection'
          )
          OR NOT aap_inference_pipelines_v1._actor_is_valid(p_request->'admitted_actor')
          OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
          OR (
              pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
              AND (p_request->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          )
          OR pg_catalog.jsonb_typeof(p_request->'filters') NOT IN ('null', 'object')
          OR pg_catalog.jsonb_typeof(p_request->'evidence') NOT IN ('null', 'object') THEN false
        WHEN p_request->>'operation' = 'list_pipelines' THEN
            pg_catalog.jsonb_typeof(p_request->'target_id') = 'null'
            AND aap_inference_pipelines_v1._has_exact_keys(
                p_request->'filters',
                ARRAY['scope', 'project_id', 'organization_id']::text[]
            )
            AND pg_catalog.jsonb_typeof(p_request #> '{filters,scope}') IN ('null', 'string')
            AND (
                pg_catalog.jsonb_typeof(p_request #> '{filters,scope}') = 'null'
                OR p_request #>> '{filters,scope}' IN ('private', 'organization', 'public')
            )
            AND pg_catalog.jsonb_typeof(p_request #> '{filters,project_id}') IN ('null', 'string')
            AND (
                pg_catalog.jsonb_typeof(p_request #> '{filters,project_id}') = 'null'
                OR (p_request #>> '{filters,project_id}') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            )
            AND pg_catalog.jsonb_typeof(p_request #> '{filters,organization_id}') IN ('null', 'string')
            AND (
                pg_catalog.jsonb_typeof(p_request #> '{filters,organization_id}') = 'null'
                OR (p_request #>> '{filters,organization_id}') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            )
            AND pg_catalog.jsonb_typeof(p_request->'evidence') = 'null'
        WHEN p_request->>'operation' = 'get_pipeline' THEN
            pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
            AND pg_catalog.jsonb_typeof(p_request->'filters') = 'null'
            AND pg_catalog.jsonb_typeof(p_request->'evidence') = 'null'
        ELSE
            pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
            AND pg_catalog.jsonb_typeof(p_request->'filters') = 'null'
            AND aap_inference_pipelines_v1._command_is_valid(p_request->'evidence')
            AND p_request #>> '{admitted_actor,id}' = p_request #>> '{evidence,admitted_actor,id}'
            AND p_request #>> '{admitted_actor,role}' = p_request #>> '{evidence,admitted_actor,role}'
            AND p_request #>> '{evidence,operation}' <> 'delete_pipeline'
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._query_is_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._pipeline_row_value(
    p_pipeline public.project_pipelines
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', (p_pipeline).id,
        'scope', (p_pipeline).scope,
        'project_id', (p_pipeline).project_id,
        'organization_id', (p_pipeline).organization_id,
        'name', (p_pipeline).name,
        'stages', (p_pipeline).stages,
        'is_default', (p_pipeline).is_default,
        'created_by', (p_pipeline).created_by,
        'usage_count', (p_pipeline).usage_count,
        'created_at', (p_pipeline).created_at,
        'updated_at', (p_pipeline).updated_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._pipeline_row_value(
    public.project_pipelines
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._step_success(
    p_request_id uuid,
    p_operation text,
    p_action text,
    p_actor_id uuid,
    p_target_id uuid,
    p_facts jsonb
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-pipelines-step/v1',
        'request_id', p_request_id,
        'operation', p_operation,
        'action', p_action,
        'actor_id', p_actor_id,
        'target_id', p_target_id,
        'ok', true,
        'facts', p_facts
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._step_success(
    uuid, text, text, uuid, uuid, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._step_failure(
    p_request_id uuid,
    p_operation text,
    p_action text,
    p_actor_id uuid,
    p_target_id uuid,
    p_status integer,
    p_detail jsonb,
    p_headers jsonb,
    p_rollback boolean
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-pipelines-step/v1',
        'request_id', p_request_id,
        'operation', p_operation,
        'action', p_action,
        'actor_id', p_actor_id,
        'target_id', p_target_id,
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'status_code', p_status,
            'detail', p_detail,
            'headers', p_headers,
            'rollback', p_rollback
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1._step_failure(
    uuid, text, text, uuid, uuid, integer, jsonb, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._guard_unfinished_operation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM aap_inference_pipelines_v1.operation_attempts AS attempt
        WHERE attempt.transaction_id = NEW.transaction_id
          AND attempt.operation_id = NEW.operation_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Inference pipeline operation was not finished',
            CONSTRAINT = 'inference_pipeline_operation_finished';
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_pipelines_v1._guard_unfinished_operation()
    FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER inference_pipeline_operation_finished
AFTER INSERT ON aap_inference_pipelines_v1.operation_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
    aap_inference_pipelines_v1._guard_unfinished_operation();
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1._cleanup_unfinished_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM aap_inference_pipelines_v1.operation_attempts AS attempt
        WHERE attempt.transaction_id = NEW.transaction_id
          AND attempt.operation_id = NEW.operation_id
    ) THEN
        DELETE FROM aap_inference_pipelines_v1.operation_states AS state
        WHERE state.transaction_id = NEW.transaction_id
          AND state.operation_id = NEW.operation_id;
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_pipelines_v1._cleanup_unfinished_state()
    FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER inference_pipeline_state_cleanup
AFTER INSERT ON aap_inference_pipelines_v1.operation_states
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
    aap_inference_pipelines_v1._cleanup_unfinished_state();
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1.pipeline_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-pipelines-policy/v1',
        'request_schema', 'aap-inference-pipelines/v1',
        'step_schema', 'aap-inference-pipelines-step/v1',
        'request_body_max_bytes', 16777216,
        'response_body_max_bytes', 16777216,
        'pipeline_columns', ARRAY[
            'id', 'scope', 'project_id', 'organization_id', 'name', 'stages',
            'is_default', 'created_by', 'usage_count', 'created_at', 'updated_at'
        ]::text[],
        'insert_columns', ARRAY[
            'id', 'scope', 'project_id', 'organization_id', 'name', 'stages',
            'is_default', 'created_by'
        ]::text[],
        'update_columns', ARRAY[
            'scope', 'project_id', 'organization_id', 'name', 'stages', 'is_default'
        ]::text[],
        'actions', ARRAY[
            'begin', 'status', 'organization_ids', 'pipeline_get', 'pipeline_list',
            'backend_get', 'backend_pool', 'backend_enabled', 'project_query',
            'project_access', 'default_clear', 'pipeline_insert', 'pipeline_update',
            'pipeline_delete', 'source_usage_increment', 'finish'
        ]::text[],
        'serialization', pg_catalog.jsonb_build_object(
            'timezone', 'UTC',
            'date_style', 'ISO,YMD',
            'extra_float_digits', 3
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1.pipeline_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_pipelines_v1.pipeline_step(
    p_request jsonb,
    p_action text,
    p_parameters jsonb,
    p_column_names text[],
    p_column_values text[]
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_kind text;
    v_operation text;
    v_reply_operation text;
    v_action text;
    v_actor_id uuid;
    v_actor_role text;
    v_target_id uuid;
    v_request_id uuid;
    v_command jsonb;
    v_evidence jsonb;
    v_request_hash text;
    v_txid bigint;
    v_state aap_inference_pipelines_v1.operation_states%ROWTYPE;
    v_receipt aap_inference_pipelines_v1.operation_receipts%ROWTYPE;
    v_state_found boolean;
    v_reply jsonb;
    v_facts jsonb;
    v_rows_json jsonb;
    v_row_json text;
    v_backend_id uuid;
    v_pool_id uuid;
    v_project_id uuid;
    v_pipeline_id uuid;
    v_resource_id uuid;
    v_source_stages jsonb;
    v_first_pool_id uuid;
    v_pipeline_exists boolean;
    v_actor_active boolean;
    v_project_exists boolean;
    v_org_ids uuid[];
    v_rows bigint;
    v_i integer;
    v_position integer;
    v_column text;
    v_cast text;
    v_assignments text;
    v_sql text;
    v_value text;
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'begin', 'status', 'organization_ids', 'pipeline_get', 'pipeline_list',
        'backend_get', 'backend_pool', 'backend_enabled', 'project_query',
        'project_access', 'default_clear', 'pipeline_insert', 'pipeline_update',
        'pipeline_delete', 'source_usage_increment', 'finish'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Inference pipeline action';
    END IF;
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_parameters IS NULL
       OR pg_catalog.jsonb_typeof(p_parameters) IS DISTINCT FROM 'object'
       OR p_column_names IS NULL OR p_column_values IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Inference pipeline step arguments';
    END IF;

    IF aap_inference_pipelines_v1._command_is_valid(p_request) THEN
        v_kind := 'command';
        v_command := p_request;
        v_operation := p_request->>'operation';
        v_reply_operation := v_operation;
        v_actor_id := (p_request #>> '{admitted_actor,id}')::uuid;
        v_actor_role := p_request #>> '{admitted_actor,role}';
        v_target_id := CASE
            WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
            THEN (p_request->>'target_id')::uuid
            ELSE NULL
        END;
        v_request_id := (p_request->>'operation_id')::uuid;
    ELSIF aap_inference_pipelines_v1._has_exact_keys(
        p_request, ARRAY['schema', 'request_id', 'operation', 'command']::text[]
    )
      AND pg_catalog.jsonb_typeof(p_request->'schema') = 'string'
      AND p_request->>'schema' = 'aap-inference-pipelines/v1'
      AND pg_catalog.jsonb_typeof(p_request->'request_id') = 'string'
      AND (p_request->>'request_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND p_request->>'operation' = 'get_operation_status'
      AND aap_inference_pipelines_v1._command_is_valid(p_request->'command') THEN
        v_kind := 'status';
        v_command := p_request->'command';
        v_operation := v_command->>'operation';
        v_reply_operation := 'get_operation_status';
        v_actor_id := (v_command #>> '{admitted_actor,id}')::uuid;
        v_actor_role := v_command #>> '{admitted_actor,role}';
        v_target_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_command->'target_id') = 'string'
            THEN (v_command->>'target_id')::uuid
            ELSE NULL
        END;
        v_request_id := (p_request->>'request_id')::uuid;
    ELSIF aap_inference_pipelines_v1._query_is_valid(p_request) THEN
        v_kind := 'query';
        v_command := NULL;
        v_operation := p_request->>'operation';
        v_reply_operation := v_operation;
        v_actor_id := (p_request #>> '{admitted_actor,id}')::uuid;
        v_actor_role := p_request #>> '{admitted_actor,role}';
        v_target_id := CASE
            WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
            THEN (p_request->>'target_id')::uuid
            ELSE NULL
        END;
        v_request_id := (p_request->>'request_id')::uuid;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Inference pipeline request';
    END IF;

    IF p_action NOT IN ('pipeline_insert', 'pipeline_update')
       AND (pg_catalog.cardinality(p_column_names) <> 0
            OR pg_catalog.cardinality(p_column_values) <> 0) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'unexpected Inference pipeline write columns';
    END IF;

    IF p_action = 'status' THEN
        IF v_kind <> 'status'
           OR NOT aap_inference_pipelines_v1._has_exact_keys(
               p_parameters, ARRAY[]::text[]
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Inference pipeline status step';
        END IF;
        v_request_hash := pg_catalog.encode(pg_catalog.sha256(
            pg_catalog.convert_to(v_command::text, 'UTF8')
        ), 'hex');
        SELECT receipt.*
        INTO v_receipt
        FROM aap_inference_pipelines_v1.operation_receipts AS receipt
        WHERE receipt.operation_id = (v_command->>'operation_id')::uuid
          AND receipt.actor_id = v_actor_id
          AND receipt.operation = v_operation
          AND receipt.target_id IS NOT DISTINCT FROM v_target_id
          AND receipt.request_hash = v_request_hash
          AND receipt.request = v_command;
        v_facts := pg_catalog.jsonb_build_object(
            'state', CASE WHEN FOUND THEN 'committed' ELSE 'unknown' END,
            'resource_id', CASE WHEN FOUND THEN pg_catalog.to_jsonb(v_receipt.resource_id) ELSE NULL END
        );
        RETURN aap_inference_pipelines_v1._step_success(
            v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id, v_facts
        );
    END IF;

    IF p_action = 'begin' THEN
        IF v_kind <> 'command'
           OR NOT aap_inference_pipelines_v1._has_exact_keys(
               p_parameters, ARRAY[]::text[]
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Inference pipeline begin step';
        END IF;
        v_request_hash := pg_catalog.encode(pg_catalog.sha256(
            pg_catalog.convert_to(v_command::text, 'UTF8')
        ), 'hex');
        SELECT receipt.*
        INTO v_receipt
        FROM aap_inference_pipelines_v1.operation_receipts AS receipt
        WHERE receipt.operation_id = v_request_id;
        IF FOUND THEN
            IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.target_id IS DISTINCT FROM v_target_id
               OR v_receipt.request_hash IS DISTINCT FROM v_request_hash
               OR v_receipt.request IS DISTINCT FROM v_command THEN
                RETURN aap_inference_pipelines_v1._step_failure(
                    v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
                    409, pg_catalog.jsonb_build_object('reason', 'operation_conflict'),
                    NULL, true
                );
            END IF;
            RETURN aap_inference_pipelines_v1._step_success(
                v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
                pg_catalog.jsonb_build_object(
                    'replay', pg_catalog.jsonb_build_object(
                        'resource_id', v_receipt.resource_id
                    )
                )
            );
        END IF;

        v_txid := pg_catalog.txid_current();
        SELECT state.*
        INTO v_state
        FROM aap_inference_pipelines_v1.operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_request_id;
        IF FOUND THEN
            RETURN aap_inference_pipelines_v1._step_failure(
                v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
                409, pg_catalog.jsonb_build_object('reason', 'operation_busy'),
                NULL, true
            );
        END IF;

        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-inference-pipelines/v1:' || v_request_id::text, 0
            )
        ) THEN
            RETURN aap_inference_pipelines_v1._step_failure(
                v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
                409, pg_catalog.jsonb_build_object('reason', 'operation_busy'),
                NULL, true
            );
        END IF;

        SELECT receipt.*
        INTO v_receipt
        FROM aap_inference_pipelines_v1.operation_receipts AS receipt
        WHERE receipt.operation_id = v_request_id;
        IF FOUND THEN
            IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.target_id IS DISTINCT FROM v_target_id
               OR v_receipt.request_hash IS DISTINCT FROM v_request_hash
               OR v_receipt.request IS DISTINCT FROM v_command THEN
                RETURN aap_inference_pipelines_v1._step_failure(
                    v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
                    409, pg_catalog.jsonb_build_object('reason', 'operation_conflict'),
                    NULL, true
                );
            END IF;
            RETURN aap_inference_pipelines_v1._step_success(
                v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
                pg_catalog.jsonb_build_object(
                    'replay', pg_catalog.jsonb_build_object(
                        'resource_id', v_receipt.resource_id
                    )
                )
            );
        END IF;

        INSERT INTO aap_inference_pipelines_v1.operation_states (
            transaction_id, operation_id, request_hash, request, actor_id,
            operation, target_id
        ) VALUES (
            v_txid, v_request_id, v_request_hash, v_command, v_actor_id,
            v_operation, v_target_id
        );
        RETURN aap_inference_pipelines_v1._step_success(
            v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id,
            pg_catalog.jsonb_build_object('replay', NULL)
        );
    END IF;

    IF v_kind = 'command' THEN
        v_request_hash := pg_catalog.encode(pg_catalog.sha256(
            pg_catalog.convert_to(v_command::text, 'UTF8')
        ), 'hex');
        SELECT state.*
        INTO v_state
        FROM aap_inference_pipelines_v1.operation_states AS state
        WHERE state.transaction_id = pg_catalog.txid_current()
          AND state.operation_id = v_request_id;
        IF NOT FOUND
           OR v_state.request_hash IS DISTINCT FROM v_request_hash
           OR v_state.request IS DISTINCT FROM v_command
           OR v_state.actor_id IS DISTINCT FROM v_actor_id
           OR v_state.operation IS DISTINCT FROM v_operation
           OR v_state.target_id IS DISTINCT FROM v_target_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Inference pipeline action has no matching begin';
        END IF;
    END IF;

    CASE p_action
        WHEN 'organization_ids' THEN
            IF NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['actor_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'actor_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (p_parameters->>'actor_id')::uuid IS DISTINCT FROM v_actor_id
               OR v_kind NOT IN ('query', 'command') THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline organization_ids step';
            END IF;
            v_reply := aap_identity_v1.workspace_template_query_org_ids(v_actor_id);
            IF pg_catalog.jsonb_typeof(v_reply) IS DISTINCT FROM 'object'
               OR v_reply->>'schema' IS DISTINCT FROM 'aap-identity-workspace-template-query/v1'
               OR v_reply->>'actor_id' IS DISTINCT FROM v_actor_id::text
               OR pg_catalog.jsonb_typeof(v_reply->'organization_ids') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Identity organization id fact';
            END IF;
            v_facts := pg_catalog.jsonb_build_object(
                'organization_ids', v_reply->'organization_ids'
            );

        WHEN 'pipeline_get' THEN
            IF NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['pipeline_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'pipeline_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'pipeline_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline pipeline_get step';
            END IF;
            v_pipeline_id := (p_parameters->>'pipeline_id')::uuid;
            IF v_kind = 'query' THEN
                IF v_operation = 'get_pipeline' THEN
                    IF v_pipeline_id IS DISTINCT FROM v_target_id THEN
                        RAISE EXCEPTION USING ERRCODE = '22023',
                            MESSAGE = 'unbound Inference pipeline detail read';
                    END IF;
                ELSIF v_operation = 'get_project_projection' THEN
                    v_evidence := p_request->'evidence';
                    SELECT receipt.*
                    INTO v_receipt
                    FROM aap_inference_pipelines_v1.operation_receipts AS receipt
                    WHERE receipt.operation_id = (v_evidence->>'operation_id')::uuid;
                    IF NOT FOUND
                       OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                       OR v_receipt.operation IS DISTINCT FROM v_evidence->>'operation'
                       OR v_receipt.target_id IS DISTINCT FROM NULLIF(v_evidence->>'target_id', '')::uuid
                       OR v_receipt.request_hash IS DISTINCT FROM pg_catalog.encode(pg_catalog.sha256(
                           pg_catalog.convert_to(v_evidence::text, 'UTF8')
                       ), 'hex')
                       OR v_receipt.request IS DISTINCT FROM v_evidence
                       OR v_receipt.resource_id IS DISTINCT FROM v_target_id
                       OR v_pipeline_id IS DISTINCT FROM v_target_id THEN
                        RAISE EXCEPTION USING ERRCODE = '22023',
                            MESSAGE = 'invalid Inference pipeline projection evidence';
                    END IF;
                ELSE
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'pipeline_get action does not match query operation';
                END IF;
            ELSIF v_kind = 'command' THEN
                IF v_operation IN ('update_pipeline', 'delete_pipeline')
                   AND v_pipeline_id IS DISTINCT FROM v_target_id THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'unbound Inference pipeline command read';
                ELSIF v_operation NOT IN ('update_pipeline', 'delete_pipeline', 'apply_pipeline') THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'pipeline_get action does not match command operation';
                END IF;
            ELSE
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'pipeline_get is unavailable for this request';
            END IF;

            v_row_json := NULL;
            v_project_id := NULL;
            v_source_stages := NULL;
            v_pipeline_exists := false;
            SELECT aap_inference_pipelines_v1._pipeline_row_value(pipeline)::text,
                   pipeline.project_id,
                   pipeline.stages
            INTO v_row_json, v_project_id, v_source_stages
            FROM public.project_pipelines AS pipeline
            WHERE pipeline.id = v_pipeline_id;
            v_pipeline_exists := FOUND;
            v_facts := pg_catalog.jsonb_build_object('row_json', v_row_json);

            IF v_kind = 'command' THEN
                IF v_operation = 'apply_pipeline' THEN
                    UPDATE aap_inference_pipelines_v1.operation_states AS state
                    SET selected_pipeline_id = v_pipeline_id,
                        selected_project_id = v_project_id,
                        selected_row_exists = v_pipeline_exists,
                        source_pipeline_id = v_pipeline_id,
                        source_stages = v_source_stages,
                        last_backend_id = NULL,
                        last_backend_found = false,
                        first_pool_id = NULL,
                        completed_actions = pg_catalog.array_append(
                            state.completed_actions, 'pipeline_get'
                        )
                    WHERE state.transaction_id = pg_catalog.txid_current()
                      AND state.operation_id = v_request_id;
                ELSE
                    UPDATE aap_inference_pipelines_v1.operation_states AS state
                    SET selected_pipeline_id = v_pipeline_id,
                        selected_project_id = v_project_id,
                        selected_row_exists = v_pipeline_exists,
                        completed_actions = pg_catalog.array_append(
                            state.completed_actions, 'pipeline_get'
                        )
                    WHERE state.transaction_id = pg_catalog.txid_current()
                      AND state.operation_id = v_request_id;
                END IF;
            END IF;

        WHEN 'pipeline_list' THEN
            IF v_kind <> 'query'
               OR v_operation <> 'list_pipelines'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters,
                   ARRAY['scope', 'project_id', 'organization_id', 'organization_ids']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'scope') NOT IN ('null', 'string')
               OR pg_catalog.jsonb_typeof(p_parameters->'project_id') NOT IN ('null', 'string')
               OR pg_catalog.jsonb_typeof(p_parameters->'organization_id') NOT IN ('null', 'string')
               OR pg_catalog.jsonb_typeof(p_parameters->'organization_ids') IS DISTINCT FROM 'array'
               OR p_parameters->'scope' IS DISTINCT FROM p_request #> '{filters,scope}'
               OR p_parameters->'project_id' IS DISTINCT FROM p_request #> '{filters,project_id}'
               OR p_parameters->'organization_id' IS DISTINCT FROM p_request #> '{filters,organization_id}' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline list step';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'organization_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline organization id list';
            END IF;
            SELECT COALESCE(
                       pg_catalog.array_agg((item.value #>> '{}')::uuid),
                       ARRAY[]::uuid[]
                   )
            INTO v_org_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'organization_ids') AS item(value);
            v_rows_json := (
                SELECT COALESCE(
                    pg_catalog.jsonb_agg(
                        aap_inference_pipelines_v1._pipeline_row_value(pipeline)
                        ORDER BY pipeline.created_at DESC
                    ),
                    '[]'::jsonb
                )
                FROM public.project_pipelines AS pipeline
                WHERE (
                    v_actor_role = 'super_admin'
                    OR pipeline.scope = 'public'
                    OR (
                        pipeline.scope = 'private'
                        AND pipeline.created_by = v_actor_id
                    )
                    OR (
                        pipeline.scope = 'organization'
                        AND pipeline.organization_id = ANY (v_org_ids)
                    )
                )
                  AND (COALESCE(p_parameters->>'scope', '') = ''
                       OR pipeline.scope = p_parameters->>'scope')
                  AND (p_parameters->'project_id' = 'null'::jsonb
                       OR pipeline.project_id = NULLIF(p_parameters->>'project_id', '')::uuid)
                  AND (p_parameters->'organization_id' = 'null'::jsonb
                       OR pipeline.organization_id = NULLIF(p_parameters->>'organization_id', '')::uuid)
            );
            v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json::text);

        WHEN 'backend_get' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'apply_pipeline'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['backend_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'backend_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'backend_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR v_state.source_pipeline_id IS NULL
               OR v_state.source_stages IS NULL
               OR pg_catalog.cardinality(v_state.access_phases) <> 0
               OR pg_catalog.jsonb_typeof(v_state.source_stages) IS DISTINCT FROM 'array'
               OR NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.jsonb_array_elements(v_state.source_stages) AS stage(value)
                   WHERE pg_catalog.jsonb_typeof(stage.value) = 'object'
                     AND stage.value->>'ml_backend_id' = p_parameters->>'backend_id'
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline backend lookup';
            END IF;
            v_backend_id := (p_parameters->>'backend_id')::uuid;
            SELECT pg_catalog.jsonb_build_object(
                       'id', registry.id,
                       'last_checked_at', registry.last_checked_at,
                       'created_at', registry.created_at,
                       'updated_at', registry.updated_at,
                       'extra_params', CASE
                           WHEN registry.extra_params IS NULL THEN NULL
                           ELSE pg_catalog.to_jsonb(registry.extra_params::text)
                       END,
                       'health_meta', CASE
                           WHEN registry.health_meta IS NULL THEN NULL
                           ELSE pg_catalog.to_jsonb(registry.health_meta::text)
                       END
                   )::text
            INTO v_row_json
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = v_backend_id;
            v_pipeline_exists := FOUND;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET last_backend_id = v_backend_id,
                last_backend_found = v_pipeline_exists,
                first_pool_id = NULL,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'backend_get'
                )
            WHERE state.transaction_id = pg_catalog.txid_current()
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('row_json', v_row_json);

        WHEN 'backend_pool' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'apply_pipeline'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['backend_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'backend_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'backend_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR v_state.last_backend_found IS NOT TRUE
               OR pg_catalog.cardinality(v_state.access_phases) <> 0
               OR v_state.last_backend_id IS DISTINCT FROM (p_parameters->>'backend_id')::uuid THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline pool lookup';
            END IF;
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                           'id', pool.id,
                           'created_at', pool.created_at,
                           'updated_at', pool.updated_at,
                           'capability_snapshot', CASE
                               WHEN pool.capability_snapshot IS NULL THEN NULL
                               ELSE pg_catalog.to_jsonb(pool.capability_snapshot::text)
                           END
                       )),
                       '[]'::jsonb
                   )
            INTO v_rows_json
            FROM public.ml_backend_service_pools AS pool
            JOIN public.ml_backend_pool_members AS member
              ON member.pool_id = pool.id
            WHERE member.registry_id = v_state.last_backend_id;
            v_first_pool_id := CASE
                WHEN pg_catalog.jsonb_array_length(v_rows_json) > 0
                THEN (v_rows_json->0->>'id')::uuid
                ELSE NULL
            END;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET first_pool_id = v_first_pool_id,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'backend_pool'
                )
            WHERE state.transaction_id = pg_catalog.txid_current()
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json::text);

        WHEN 'backend_enabled' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'apply_pipeline'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['project_id', 'pool_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_parameters->'pool_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (p_parameters->>'pool_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (p_parameters->>'project_id')::uuid IS DISTINCT FROM v_target_id
               OR v_state.first_pool_id IS NULL
               OR pg_catalog.cardinality(v_state.access_phases) <> 0
               OR (p_parameters->>'pool_id')::uuid IS DISTINCT FROM v_state.first_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline enabled association lookup';
            END IF;
            SELECT pg_catalog.jsonb_build_object(
                       'id', association.id,
                       'project_id', association.project_id,
                       'pool_id', association.pool_id,
                       'enabled', association.enabled,
                       'created_at', association.created_at,
                       'updated_at', association.updated_at,
                       'default_variants', CASE
                           WHEN association.default_variants IS NULL THEN NULL
                           ELSE pg_catalog.to_jsonb(association.default_variants::text)
                       END
                   )::text
            INTO v_row_json
            FROM public.project_ml_backend_pool AS association
            WHERE association.project_id = v_target_id
              AND association.pool_id = v_state.first_pool_id;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'backend_enabled'
                )
            WHERE state.transaction_id = pg_catalog.txid_current()
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('row_json', v_row_json);

        WHEN 'project_query' THEN
            IF v_kind <> 'command'
               OR v_operation NOT IN ('create_pipeline', 'update_pipeline')
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['request', 'action']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'request') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(p_parameters->'action') IS DISTINCT FROM 'string'
               OR p_parameters->>'action' NOT IN (
                   'get_row', 'actor_status', 'access_member_rows'
               )
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters->'request',
                   ARRAY[
                       'schema', 'request_id', 'operation', 'admitted_actor',
                       'target_id', 'filters_ascii', 'pagination', 'projection_evidence'
                   ]::text[]
               )
               OR p_parameters #>> '{request,schema}' IS DISTINCT FROM 'aap-workspace-project-query/v1'
               OR p_parameters #>> '{request,operation}' IS DISTINCT FROM 'require_project_visible'
               OR pg_catalog.jsonb_typeof(p_parameters #> '{request,request_id}') IS DISTINCT FROM 'string'
               OR (p_parameters #>> '{request,request_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR NOT aap_inference_pipelines_v1._actor_is_valid(
                   p_parameters #> '{request,admitted_actor}'
               )
               OR (p_parameters #>> '{request,admitted_actor,id}')::uuid IS DISTINCT FROM v_actor_id
               OR p_parameters #>> '{request,admitted_actor,role}' IS DISTINCT FROM v_actor_role
               OR pg_catalog.jsonb_typeof(p_parameters #> '{request,target_id}') IS DISTINCT FROM 'string'
               OR (p_parameters #>> '{request,target_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_parameters #> '{request,filters_ascii}') IS DISTINCT FROM 'null'
               OR pg_catalog.jsonb_typeof(p_parameters #> '{request,pagination}') IS DISTINCT FROM 'null'
               OR pg_catalog.jsonb_typeof(p_parameters #> '{request,projection_evidence}') IS DISTINCT FROM 'null' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline Project query bridge';
            END IF;
            v_project_id := (p_parameters #>> '{request,target_id}')::uuid;
            v_request_id := v_state.operation_id;
            IF v_state.visibility_project_id IS NOT NULL
               AND v_state.visibility_project_id IS DISTINCT FROM v_project_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Inference pipeline Project visibility target changed';
            END IF;
            IF v_state.visibility_request_id IS NOT NULL
               AND v_state.visibility_request_id IS DISTINCT FROM
                   (p_parameters #>> '{request,request_id}')::uuid THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Inference pipeline Project visibility request changed';
            END IF;

            IF p_parameters->>'action' = 'get_row' THEN
                IF NOT (
                    v_state.visibility_get_count = 0
                    OR (
                        v_state.visibility_get_count = 1
                        AND v_state.visibility_actor_seen
                        AND v_state.visibility_actor_active
                    )
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'out-of-order Inference pipeline Project read';
                END IF;
            ELSIF p_parameters->>'action' = 'actor_status' THEN
                IF v_state.visibility_get_count <> 1
                   OR v_state.visibility_actor_seen
                   OR NOT v_state.visibility_project_exists THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'out-of-order Inference pipeline actor read';
                END IF;
            ELSE
                IF v_state.visibility_get_count <> 2
                   OR NOT v_state.visibility_actor_seen
                   OR NOT v_state.visibility_actor_active
                   OR NOT v_state.visibility_project_exists
                   OR v_state.visibility_member_seen THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'out-of-order Inference pipeline membership read';
                END IF;
            END IF;

            v_reply := aap_workspace_project_query_v1.project_query_step(
                p_parameters->'request',
                p_parameters->>'action',
                NULL::jsonb,
                NULL::text,
                NULL::text,
                NULL::text[],
                NULL::text[],
                NULL::uuid,
                NULL::text,
                NULL::timestamptz,
                NULL::timestamptz,
                NULL::bigint,
                NULL::integer
            );
            IF p_parameters->>'action' = 'get_row' THEN
                v_project_exists :=
                    pg_catalog.jsonb_typeof(v_reply #> '{facts,row_json}') = 'string';
                UPDATE aap_inference_pipelines_v1.operation_states AS state
                SET visibility_project_id = v_project_id,
                    visibility_request_id = (p_parameters #>> '{request,request_id}')::uuid,
                    visibility_get_count = state.visibility_get_count + 1,
                    visibility_project_exists = v_project_exists,
                    completed_actions = pg_catalog.array_append(
                        state.completed_actions, 'project_query'
                    )
                WHERE state.transaction_id = pg_catalog.txid_current()
                  AND state.operation_id = v_state.operation_id;
            ELSIF p_parameters->>'action' = 'actor_status' THEN
                v_actor_active := COALESCE(
                    (v_reply #>> '{facts,actor,is_active}')::boolean,
                    false
                );
                UPDATE aap_inference_pipelines_v1.operation_states AS state
                SET visibility_actor_seen = true,
                    visibility_actor_active = v_actor_active,
                    completed_actions = pg_catalog.array_append(
                        state.completed_actions, 'project_query'
                    )
                WHERE state.transaction_id = pg_catalog.txid_current()
                  AND state.operation_id = v_state.operation_id;
            ELSE
                UPDATE aap_inference_pipelines_v1.operation_states AS state
                SET visibility_member_seen = true,
                    completed_actions = pg_catalog.array_append(
                        state.completed_actions, 'project_query'
                    )
                WHERE state.transaction_id = pg_catalog.txid_current()
                  AND state.operation_id = v_state.operation_id;
            END IF;
            v_facts := pg_catalog.jsonb_build_object('workspace_reply', v_reply);

        WHEN 'project_access' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'apply_pipeline'
               OR v_state.source_pipeline_id IS NULL
               OR NOT v_state.selected_row_exists
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['phase']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'phase') IS DISTINCT FROM 'string'
               OR p_parameters->>'phase' NOT IN (
                   'lock_actor_share', 'lock_project_key_share', 'lock_member_share'
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline Project access step';
            END IF;
            IF (
                p_parameters->>'phase' = 'lock_actor_share'
                AND pg_catalog.cardinality(v_state.access_phases) <> 0
            ) OR (
                p_parameters->>'phase' = 'lock_project_key_share'
                AND (
                    v_state.access_phases IS DISTINCT FROM ARRAY['lock_actor_share']::text[]
                    OR NOT v_state.access_actor_active
                )
            ) OR (
                p_parameters->>'phase' = 'lock_member_share'
                AND (
                    NOT (
                        v_state.access_phases =
                            ARRAY['lock_actor_share', 'lock_project_key_share']::text[]
                        OR v_state.access_phases = ARRAY[
                            'lock_actor_share', 'lock_project_key_share', 'lock_member_share'
                        ]::text[]
                    )
                    OR NOT v_state.access_actor_active
                    OR NOT v_state.access_project_exists
                )
            ) OR p_parameters->>'phase' = ANY(v_state.access_phases) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'out-of-order Inference pipeline Project access phase';
            END IF;
            v_reply := aap_workspace_v1.project_access_step(
                v_actor_id, v_target_id, p_parameters->>'phase'
            );
            IF p_parameters->>'phase' = 'lock_actor_share' THEN
                v_actor_active := COALESCE(
                    (v_reply #>> '{facts,actor,is_active}')::boolean,
                    false
                );
                UPDATE aap_inference_pipelines_v1.operation_states AS state
                SET access_actor_active = v_actor_active,
                    access_phases = pg_catalog.array_append(
                        state.access_phases, p_parameters->>'phase'
                    ),
                    completed_actions = pg_catalog.array_append(
                        state.completed_actions, 'project_access'
                    )
                WHERE state.transaction_id = pg_catalog.txid_current()
                  AND state.operation_id = v_state.operation_id;
            ELSIF p_parameters->>'phase' = 'lock_project_key_share' THEN
                v_project_exists :=
                    pg_catalog.jsonb_typeof(v_reply #> '{facts,row_json}') = 'string';
                UPDATE aap_inference_pipelines_v1.operation_states AS state
                SET access_project_exists = v_project_exists,
                    access_phases = pg_catalog.array_append(
                        state.access_phases, p_parameters->>'phase'
                    ),
                    completed_actions = pg_catalog.array_append(
                        state.completed_actions, 'project_access'
                    )
                WHERE state.transaction_id = pg_catalog.txid_current()
                  AND state.operation_id = v_state.operation_id;
            ELSE
                UPDATE aap_inference_pipelines_v1.operation_states AS state
                SET access_phases = pg_catalog.array_append(
                        state.access_phases, p_parameters->>'phase'
                    ),
                    completed_actions = pg_catalog.array_append(
                        state.completed_actions, 'project_access'
                    )
                WHERE state.transaction_id = pg_catalog.txid_current()
                  AND state.operation_id = v_state.operation_id;
            END IF;
            v_facts := pg_catalog.jsonb_build_object('workspace_reply', v_reply);

        WHEN 'default_clear' THEN
            IF v_kind <> 'command'
               OR v_operation NOT IN ('create_pipeline', 'update_pipeline', 'apply_pipeline')
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['project_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline default_clear step';
            END IF;
            v_project_id := (p_parameters->>'project_id')::uuid;
            IF v_operation = 'apply_pipeline' THEN
                IF v_project_id IS DISTINCT FROM v_target_id
                   OR v_state.access_phases[1:2] IS DISTINCT FROM
                       ARRAY['lock_actor_share', 'lock_project_key_share']::text[]
                   OR NOT v_state.source_usage_seen THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'unbound Inference pipeline default clear';
                END IF;
            ELSIF v_state.visibility_project_id IS DISTINCT FROM v_project_id
               OR v_state.visibility_get_count <> 2
               OR NOT v_state.visibility_actor_seen
               OR NOT v_state.visibility_actor_active
               OR NOT v_state.visibility_project_exists THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline default clear';
            END IF;
            INSERT INTO aap_inference_pipelines_v1.operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (
                v_state.transaction_id, v_request_id, v_request_hash
            ) ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            UPDATE public.project_pipelines AS pipeline
            SET is_default = false,
                updated_at = pg_catalog.now()
            WHERE pipeline.project_id = v_project_id
              AND pipeline.is_default IS TRUE;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET default_cleared_project_id = v_project_id,
                default_clear_seen = true,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'default_clear'
                )
            WHERE state.transaction_id = v_state.transaction_id
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('affected_count', v_rows);

        WHEN 'pipeline_insert' THEN
            IF v_kind <> 'command'
               OR v_operation NOT IN ('create_pipeline', 'apply_pipeline')
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY[]::text[]
               )
               OR pg_catalog.cardinality(p_column_names) <> 8
               OR pg_catalog.cardinality(p_column_values) <> 8
               OR pg_catalog.array_lower(p_column_names, 1) IS DISTINCT FROM 1
               OR pg_catalog.array_lower(p_column_values, 1) IS DISTINCT FROM 1
               OR pg_catalog.array_upper(p_column_names, 1) IS DISTINCT FROM 8
               OR pg_catalog.array_upper(p_column_values, 1) IS DISTINCT FROM 8
               OR p_column_names IS DISTINCT FROM ARRAY[
                   'id', 'scope', 'project_id', 'organization_id', 'name', 'stages',
                   'is_default', 'created_by'
               ]::text[] THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline insert columns';
            END IF;
            IF p_column_values[1] IS NULL
               OR p_column_values[2] IS NULL
               OR p_column_values[5] IS NULL
               OR p_column_values[6] IS NULL
               OR p_column_values[7] IS NULL
               OR p_column_values[7] NOT IN ('true', 'false')
               OR p_column_values[8] IS NULL
               OR p_column_values[1] !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR p_column_values[8] !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline insert values';
            END IF;
            v_pipeline_id := p_column_values[1]::uuid;
            v_project_id := CASE
                WHEN p_column_values[3] IS NULL THEN NULL
                ELSE p_column_values[3]::uuid
            END;
            v_pool_id := CASE
                WHEN p_column_values[4] IS NULL THEN NULL
                ELSE p_column_values[4]::uuid
            END;
            IF p_column_values[8]::uuid IS DISTINCT FROM v_actor_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline creator';
            END IF;
            IF v_operation = 'apply_pipeline' THEN
                IF v_project_id IS DISTINCT FROM v_target_id
                   OR p_column_values[2] IS DISTINCT FROM 'private'
                   OR v_pool_id IS NOT NULL
                   OR v_state.source_pipeline_id IS NULL
                   OR NOT v_state.source_usage_seen
                   OR NOT v_state.access_actor_active
                   OR NOT v_state.access_project_exists
                   OR v_state.access_phases[1:2] IS DISTINCT FROM
                       ARRAY['lock_actor_share', 'lock_project_key_share']::text[] THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'unbound Inference pipeline copy insert';
                END IF;
            ELSIF v_project_id IS NOT NULL
               AND (
                   v_state.visibility_project_id IS DISTINCT FROM v_project_id
                   OR v_state.visibility_get_count <> 2
                   OR NOT v_state.visibility_actor_seen
                   OR NOT v_state.visibility_actor_active
                   OR NOT v_state.visibility_project_exists
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline Project insert';
            END IF;
            IF p_column_values[7] = 'true'
               AND (
                   v_project_id IS NULL
                   OR p_column_values[2] IS DISTINCT FROM 'private'
                   OR v_state.default_clear_seen IS NOT TRUE
                   OR v_state.default_cleared_project_id IS DISTINCT FROM v_project_id
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'default pipeline insert was not preceded by a bound clear';
            END IF;

            INSERT INTO aap_inference_pipelines_v1.operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (
                v_state.transaction_id, v_request_id, v_request_hash
            ) ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            INSERT INTO public.project_pipelines (
                id, scope, project_id, organization_id, name, stages,
                is_default, created_by
            ) VALUES (
                p_column_values[1]::uuid,
                p_column_values[2]::varchar,
                CASE WHEN p_column_values[3] IS NULL THEN NULL ELSE p_column_values[3]::uuid END,
                CASE WHEN p_column_values[4] IS NULL THEN NULL ELSE p_column_values[4]::uuid END,
                p_column_values[5]::varchar,
                p_column_values[6]::jsonb,
                p_column_values[7]::boolean,
                p_column_values[8]::uuid
            ) RETURNING id INTO v_resource_id;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET resource_id = v_resource_id,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'pipeline_insert'
                )
            WHERE state.transaction_id = v_state.transaction_id
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('resource_id', v_resource_id);

        WHEN 'pipeline_update' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'update_pipeline'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['pipeline_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'pipeline_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'pipeline_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (p_parameters->>'pipeline_id')::uuid IS DISTINCT FROM v_target_id
               OR v_state.selected_pipeline_id IS DISTINCT FROM v_target_id
               OR NOT v_state.selected_row_exists
               OR pg_catalog.cardinality(p_column_names) <> pg_catalog.cardinality(p_column_values)
               OR pg_catalog.cardinality(p_column_names) > 6
               OR (
                   pg_catalog.cardinality(p_column_names) > 0
                   AND (
                       pg_catalog.array_lower(p_column_names, 1) IS DISTINCT FROM 1
                       OR pg_catalog.array_lower(p_column_values, 1) IS DISTINCT FROM 1
                       OR pg_catalog.array_upper(p_column_names, 1) IS DISTINCT FROM pg_catalog.cardinality(p_column_names)
                       OR pg_catalog.array_upper(p_column_values, 1) IS DISTINCT FROM pg_catalog.cardinality(p_column_values)
                   )
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline update step';
            END IF;
            v_assignments := '';
            v_position := 0;
            FOR v_i IN 1..pg_catalog.cardinality(p_column_names) LOOP
                v_column := p_column_names[v_i];
                v_value := p_column_values[v_i];
                v_position := CASE v_column
                    WHEN 'scope' THEN 1
                    WHEN 'project_id' THEN 2
                    WHEN 'organization_id' THEN 3
                    WHEN 'name' THEN 4
                    WHEN 'stages' THEN 5
                    WHEN 'is_default' THEN 6
                    ELSE 0
                END;
                IF v_position = 0
                   OR EXISTS (
                       SELECT 1
                       FROM pg_catalog.generate_series(1, v_i - 1) AS prior(i)
                       WHERE p_column_names[prior.i] = v_column
                   )
                   OR (
                       v_i > 1
                       AND v_position <= CASE p_column_names[v_i - 1]
                           WHEN 'scope' THEN 1
                           WHEN 'project_id' THEN 2
                           WHEN 'organization_id' THEN 3
                           WHEN 'name' THEN 4
                           WHEN 'stages' THEN 5
                           WHEN 'is_default' THEN 6
                           ELSE 0
                       END
                   ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'invalid Inference pipeline update column order';
                END IF;
                IF (v_column = 'scope' AND v_value IS NULL)
                   OR (v_column = 'name' AND v_value IS NULL)
                   OR (v_column = 'stages' AND v_value IS NULL)
                   OR (v_column = 'is_default'
                       AND (v_value IS NULL OR v_value NOT IN ('true', 'false')))
                   OR (v_column IN ('project_id', 'organization_id')
                       AND v_value IS NOT NULL
                       AND v_value !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'invalid Inference pipeline update value';
                END IF;
                v_cast := CASE v_column
                    WHEN 'scope' THEN 'varchar'
                    WHEN 'project_id' THEN 'uuid'
                    WHEN 'organization_id' THEN 'uuid'
                    WHEN 'name' THEN 'varchar'
                    WHEN 'stages' THEN 'jsonb'
                    WHEN 'is_default' THEN 'boolean'
                END;
                v_assignments := v_assignments || CASE
                    WHEN v_assignments = '' THEN '' ELSE ', '
                END || pg_catalog.format(
                    '%I = ($1::text[])[%s]::%s', v_column, v_i, v_cast
                );
            END LOOP;

            v_project_id := CASE
                WHEN 'project_id' = ANY (p_column_names)
                    THEN CASE
                        WHEN p_column_values[pg_catalog.array_position(p_column_names, 'project_id')] IS NULL
                        THEN NULL
                        ELSE p_column_values[pg_catalog.array_position(p_column_names, 'project_id')]::uuid
                    END
                ELSE v_state.selected_project_id
            END;
            IF v_project_id IS NOT NULL
               AND (
                   v_state.visibility_project_id IS DISTINCT FROM v_project_id
                   OR v_state.visibility_get_count <> 2
                   OR NOT v_state.visibility_actor_seen
                   OR NOT v_state.visibility_actor_active
                   OR NOT v_state.visibility_project_exists
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline Project update';
            END IF;
            IF 'is_default' = ANY (p_column_names)
               AND p_column_values[pg_catalog.array_position(p_column_names, 'is_default')] = 'true'
               AND (
                   v_project_id IS NULL
                   OR v_state.default_clear_seen IS NOT TRUE
                   OR v_state.default_cleared_project_id IS DISTINCT FROM v_project_id
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'default pipeline update was not preceded by a bound clear';
            END IF;

            IF pg_catalog.cardinality(p_column_names) = 0 THEN
                v_resource_id := v_target_id;
            ELSE
                INSERT INTO aap_inference_pipelines_v1.operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (
                    v_state.transaction_id, v_request_id, v_request_hash
                ) ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                v_sql := pg_catalog.format(
                    'UPDATE public.project_pipelines SET %s, updated_at = pg_catalog.now() WHERE id = $2 RETURNING id',
                    v_assignments
                );
                EXECUTE v_sql INTO v_resource_id USING p_column_values, v_target_id;
                IF v_resource_id IS DISTINCT FROM v_target_id THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Inference pipeline update target disappeared';
                END IF;
            END IF;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET resource_id = v_resource_id,
                update_seen = true,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'pipeline_update'
                )
            WHERE state.transaction_id = v_state.transaction_id
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('resource_id', v_resource_id);

        WHEN 'pipeline_delete' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'delete_pipeline'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['pipeline_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'pipeline_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'pipeline_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (p_parameters->>'pipeline_id')::uuid IS DISTINCT FROM v_target_id
               OR v_state.selected_pipeline_id IS DISTINCT FROM v_target_id
               OR NOT v_state.selected_row_exists THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline delete step';
            END IF;
            INSERT INTO aap_inference_pipelines_v1.operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (
                v_state.transaction_id, v_request_id, v_request_hash
            ) ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            DELETE FROM public.project_pipelines AS pipeline
            WHERE pipeline.id = v_target_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Inference pipeline delete target disappeared';
            END IF;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET resource_id = v_target_id,
                delete_seen = true,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'pipeline_delete'
                )
            WHERE state.transaction_id = v_state.transaction_id
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('resource_id', v_target_id);

        WHEN 'source_usage_increment' THEN
            IF v_kind <> 'command'
               OR v_operation <> 'apply_pipeline'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['source_pipeline_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'source_pipeline_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'source_pipeline_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR v_state.source_pipeline_id IS NULL
               OR (p_parameters->>'source_pipeline_id')::uuid IS DISTINCT FROM v_state.source_pipeline_id
               OR NOT v_state.access_actor_active
               OR NOT v_state.access_project_exists
               OR v_state.access_phases[1:2] IS DISTINCT FROM
                   ARRAY['lock_actor_share', 'lock_project_key_share']::text[] THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'unbound Inference pipeline source usage increment';
            END IF;
            INSERT INTO aap_inference_pipelines_v1.operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (
                v_state.transaction_id, v_request_id, v_request_hash
            ) ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            UPDATE public.project_pipelines AS pipeline
            SET usage_count = COALESCE(pipeline.usage_count, 0) + 1,
                updated_at = pg_catalog.now()
            WHERE pipeline.id = v_state.source_pipeline_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            UPDATE aap_inference_pipelines_v1.operation_states AS state
            SET source_usage_seen = true,
                completed_actions = pg_catalog.array_append(
                    state.completed_actions, 'source_usage_increment'
                )
            WHERE state.transaction_id = v_state.transaction_id
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('affected_count', v_rows);

        WHEN 'finish' THEN
            IF v_kind <> 'command'
               OR NOT aap_inference_pipelines_v1._has_exact_keys(
                   p_parameters, ARRAY['resource_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'resource_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'resource_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Inference pipeline finish step';
            END IF;
            v_resource_id := (p_parameters->>'resource_id')::uuid;
            IF v_state.resource_id IS DISTINCT FROM v_resource_id
               OR (
                   v_operation = 'create_pipeline'
                   AND NOT ('pipeline_insert' = ANY(v_state.completed_actions))
               ) OR (
                   v_operation = 'update_pipeline'
                   AND (
                       NOT v_state.update_seen
                       OR v_resource_id IS DISTINCT FROM v_target_id
                   )
               ) OR (
                   v_operation = 'delete_pipeline'
                   AND NOT v_state.delete_seen
               ) OR (
                   v_operation = 'apply_pipeline'
                   AND (
                       NOT ('pipeline_insert' = ANY(v_state.completed_actions))
                       OR NOT v_state.source_usage_seen
                       OR NOT v_state.access_actor_active
                       OR NOT v_state.access_project_exists
                       OR v_state.access_phases[1:2] IS DISTINCT FROM
                           ARRAY['lock_actor_share', 'lock_project_key_share']::text[]
                   )
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Inference pipeline finish does not match completed command';
            END IF;
            INSERT INTO aap_inference_pipelines_v1.operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (
                v_state.transaction_id, v_request_id, v_request_hash
            ) ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            INSERT INTO aap_inference_pipelines_v1.operation_receipts (
                operation_id, actor_id, operation, target_id, request_hash,
                request, resource_id
            ) VALUES (
                v_request_id, v_actor_id, v_operation, v_target_id,
                v_request_hash, v_command, v_resource_id
            );
            DELETE FROM aap_inference_pipelines_v1.operation_attempts AS attempt
            WHERE attempt.transaction_id = v_state.transaction_id
              AND attempt.operation_id = v_request_id;
            DELETE FROM aap_inference_pipelines_v1.operation_states AS state
            WHERE state.transaction_id = v_state.transaction_id
              AND state.operation_id = v_request_id;
            v_facts := pg_catalog.jsonb_build_object('resource_id', v_resource_id);

        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Inference pipeline action is not implemented for this request';
    END CASE;

    RETURN aap_inference_pipelines_v1._step_success(
        v_request_id, v_reply_operation, p_action, v_actor_id, v_target_id, v_facts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1.pipeline_step(
    jsonb, text, jsonb, text[], text[]
) FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_pipelines_v1.pipeline_policy() IS
    'Fixed named-pipeline NativeSQL capability policy; clients receive no table access.';
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_pipelines_v1.pipeline_step(
    jsonb, text, jsonb, text[], text[]
) IS
    'Closed typed pipeline action dispatcher; project authority is delegated to fixed Identity and Workspace capabilities.';
