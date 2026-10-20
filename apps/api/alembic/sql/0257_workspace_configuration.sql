-- Workspace-owned, transaction-local execution of project/template configuration.
CREATE SCHEMA aap_workspace_configuration_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_configuration_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.workspace_configuration_operation_states (
    transaction_id bigint NOT NULL,
    operation_id uuid NOT NULL,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    request jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(request) = 'object'),
    facts jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(facts) = 'object'),
    completed_actions text[] NOT NULL DEFAULT ARRAY[]::text[],
    resource_id uuid,
    display_id text,
    template_id uuid,
    primary_pool_id uuid,
    binding_pending boolean NOT NULL DEFAULT false,
    source_backend_id uuid,
    source_backend_ready boolean NOT NULL DEFAULT false,
    authorized boolean NOT NULL DEFAULT false,
    source_project_id uuid,
    target_id uuid,
    dataset_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
    media_item_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (transaction_id, operation_id)
);
-- aap-statement-break
COMMENT ON TABLE public.workspace_configuration_operation_states IS
    'Private per-transaction phase binding for one Workspace configuration operation; no foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE public.workspace_configuration_operation_states FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.workspace_configuration_operation_attempts (
    transaction_id bigint NOT NULL,
    operation_id uuid NOT NULL,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (transaction_id, operation_id)
);
-- aap-statement-break
COMMENT ON TABLE public.workspace_configuration_operation_attempts IS
    'Private write-attempt markers; a deferred constraint trigger forbids committing before finish.';
-- aap-statement-break
REVOKE ALL ON TABLE public.workspace_configuration_operation_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.workspace_configuration_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation IN (
        'create_project', 'update_project', 'create_template',
        'update_template', 'delete_template', 'duplicate_template'
    )),
    target_id uuid,
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    selectors jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(selectors) = 'object'),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    frozen_scope text,
    frozen_organization_id uuid,
    frozen_creator_id uuid,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
COMMENT ON TABLE public.workspace_configuration_operation_receipts IS
    'Immutable successful Workspace configuration replies and deleted-template replay facts; no foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE public.workspace_configuration_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._has_exact_keys(
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
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._configuration_actor(
    p_actor_id uuid,
    p_locked boolean,
    p_busy_marker text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_actor jsonb;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_original_hint text;
BEGIN
    IF p_actor_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF p_locked THEN
        BEGIN
            v_reply := aap_identity_v1.lock_accounts_share(
                ARRAY[p_actor_id]::uuid[], true
            );
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS
                v_sqlstate = RETURNED_SQLSTATE,
                v_message = MESSAGE_TEXT,
                v_detail = PG_EXCEPTION_DETAIL,
                v_original_hint = PG_EXCEPTION_HINT;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.configuration.' || COALESCE(
                    NULLIF(p_busy_marker, ''), 'account_busy'
                ) || CASE
                    WHEN v_original_hint IS NULL OR v_original_hint = '' THEN ''
                    ELSE E'\n' || v_original_hint
                END;
        END;
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_reply, ARRAY['schema', 'accounts']::text[]
        ) OR v_reply->>'schema' IS DISTINCT FROM 'aap-identity-account-locks/v1'
          OR pg_catalog.jsonb_typeof(v_reply->'accounts') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Identity lock reply';
        END IF;
        SELECT account.value
        INTO v_actor
        FROM pg_catalog.jsonb_array_elements(v_reply->'accounts') AS account(value)
        WHERE account.value->>'id' = p_actor_id::text;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_actor, ARRAY['id', 'name', 'email', 'role', 'is_active']::text[]
        ) OR pg_catalog.jsonb_typeof(v_actor->'name') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(v_actor->'email') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(v_actor->'role') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(v_actor->'is_active') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Identity lock actor';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'id', p_actor_id,
            'role', v_actor->'role',
            'is_active', v_actor->'is_active',
            'name', v_actor->'name',
            'email', v_actor->'email'
        );
    END IF;

    SELECT pg_catalog.jsonb_build_object(
        'id', actor.actor_id,
        'role', actor.platform_role,
        'is_active', actor.is_active
    )
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS actor;
    RETURN v_actor;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._configuration_actor(
    uuid, boolean, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._configuration_project_snapshot(
    p_project_id uuid,
    p_lock_mode text
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
    v_project jsonb;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_original_hint text;
BEGIN
    IF p_project_id IS NULL OR p_lock_mode NOT IN ('none', 'key_share') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project snapshot request';
    END IF;
    IF p_lock_mode = 'key_share' THEN
        BEGIN
            SELECT pg_catalog.jsonb_build_object(
                'id', project.id,
                'organization_id', project.organization_id,
                'display_id', project.display_id,
                'name', project.name,
                'type_label', project.type_label,
                'type_key', project.type_key,
                'data_type', project.data_type,
                'owner_id', project.owner_id,
                'status', project.status,
                'ai_enabled', project.ai_enabled,
                'ml_backend_pool_id', project.ml_backend_pool_id,
                'default_variants', project.default_variants,
                'preannotate_pipeline', project.preannotate_pipeline,
                'label_config', project.label_config,
                'tool_bindings', project.tool_bindings,
                'sampling', project.sampling,
                'maximum_annotations', project.maximum_annotations,
                'show_overlap_first', project.show_overlap_first,
                'iou_dedup_threshold', project.iou_dedup_threshold::double precision,
                'box_threshold', project.box_threshold::double precision,
                'text_threshold', project.text_threshold::double precision,
                'ai_interactive_enabled', project.ai_interactive_enabled,
                'raster_mask_native_editing_enabled', project.raster_mask_native_editing_enabled,
                'mask_qc_config', project.mask_qc_config,
                'point_cloud_quality_config', project.point_cloud_quality_config,
                'rendering_config', project.rendering_config,
                'video_sampling', project.video_sampling,
                'video_collaboration', project.video_collaboration,
                'scene_mode', project.scene_mode,
                'prefer_same_scene_continuation', project.prefer_same_scene_continuation,
                'scene_continuation_window_min', project.scene_continuation_window_min,
                'annotation_guide', project.annotation_guide,
                'guide_assets', project.guide_assets,
                'model_version', project.model_version,
                'task_lock_ttl_seconds', project.task_lock_ttl_seconds,
                'due_date', project.due_date,
                'created_at', project.created_at,
                'updated_at', project.updated_at,
                'total_tasks', aap_labeling_v1.workspace_configuration_task_count(
                    project.id
                )->'total_tasks'
            )
            INTO v_project
            FROM public.projects AS project
            WHERE project.id = p_project_id
            FOR KEY SHARE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS
                v_sqlstate = RETURNED_SQLSTATE,
                v_message = MESSAGE_TEXT,
                v_detail = PG_EXCEPTION_DETAIL,
                v_original_hint = PG_EXCEPTION_HINT;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.configuration.project_busy' || CASE
                    WHEN v_original_hint IS NULL OR v_original_hint = '' THEN ''
                    ELSE E'\n' || v_original_hint
                END;
        END;
    ELSE
        SELECT pg_catalog.jsonb_build_object(
            'id', project.id,
            'organization_id', project.organization_id,
            'display_id', project.display_id,
            'name', project.name,
            'type_label', project.type_label,
            'type_key', project.type_key,
            'data_type', project.data_type,
            'owner_id', project.owner_id,
            'status', project.status,
            'ai_enabled', project.ai_enabled,
            'ml_backend_pool_id', project.ml_backend_pool_id,
            'default_variants', project.default_variants,
            'preannotate_pipeline', project.preannotate_pipeline,
            'label_config', project.label_config,
            'tool_bindings', project.tool_bindings,
            'sampling', project.sampling,
            'maximum_annotations', project.maximum_annotations,
            'show_overlap_first', project.show_overlap_first,
            'iou_dedup_threshold', project.iou_dedup_threshold::double precision,
            'box_threshold', project.box_threshold::double precision,
            'text_threshold', project.text_threshold::double precision,
            'ai_interactive_enabled', project.ai_interactive_enabled,
            'raster_mask_native_editing_enabled', project.raster_mask_native_editing_enabled,
            'mask_qc_config', project.mask_qc_config,
            'point_cloud_quality_config', project.point_cloud_quality_config,
            'rendering_config', project.rendering_config,
            'video_sampling', project.video_sampling,
            'video_collaboration', project.video_collaboration,
            'scene_mode', project.scene_mode,
            'prefer_same_scene_continuation', project.prefer_same_scene_continuation,
            'scene_continuation_window_min', project.scene_continuation_window_min,
            'annotation_guide', project.annotation_guide,
            'guide_assets', project.guide_assets,
            'model_version', project.model_version,
            'task_lock_ttl_seconds', project.task_lock_ttl_seconds,
            'due_date', project.due_date,
            'created_at', project.created_at,
            'updated_at', project.updated_at,
            'total_tasks', aap_labeling_v1.workspace_configuration_task_count(
                project.id
            )->'total_tasks'
        )
        INTO v_project
        FROM public.projects AS project
        WHERE project.id = p_project_id;
    END IF;
    RETURN v_project;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._configuration_project_snapshot(
    uuid, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._configuration_project_access(
    p_actor jsonb,
    p_project jsonb,
    p_lock_membership boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_id uuid;
    v_project_id uuid;
    v_platform_role text;
    v_project_role text;
    v_owner_id uuid;
    v_found boolean;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_original_hint text;
BEGIN
    IF p_actor IS NULL OR p_project IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'visible', false, 'is_manager', false, 'code', 'project_missing'
        );
    END IF;
    v_actor_id := (p_actor->>'id')::uuid;
    v_platform_role := p_actor->>'role';
    v_project_id := (p_project->>'id')::uuid;
    v_owner_id := (p_project->>'owner_id')::uuid;
    IF v_platform_role IS NULL OR v_platform_role NOT IN (
        'super_admin', 'project_admin', 'employee', 'viewer'
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'visible', true, 'is_manager', false, 'code', 'invalid_account_role'
        );
    END IF;
    IF v_platform_role = 'super_admin'
       OR (v_platform_role = 'project_admin' AND v_owner_id = v_actor_id) THEN
        RETURN pg_catalog.jsonb_build_object(
            'visible', true, 'is_manager', true, 'code', NULL
        );
    END IF;

    IF p_lock_membership THEN
        BEGIN
            SELECT member.role
            INTO v_project_role
            FROM public.project_members AS member
            WHERE member.project_id = v_project_id
              AND member.user_id = v_actor_id
            FOR SHARE OF member NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS
                v_sqlstate = RETURNED_SQLSTATE,
                v_message = MESSAGE_TEXT,
                v_detail = PG_EXCEPTION_DETAIL,
                v_original_hint = PG_EXCEPTION_HINT;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.configuration.membership_busy' || CASE
                    WHEN v_original_hint IS NULL OR v_original_hint = '' THEN ''
                    ELSE E'\n' || v_original_hint
                END;
        END;
    ELSE
        SELECT member.role
        INTO v_project_role
        FROM public.project_members AS member
        WHERE member.project_id = v_project_id
          AND member.user_id = v_actor_id;
    END IF;
    v_found := FOUND;
    IF NOT v_found THEN
        RETURN pg_catalog.jsonb_build_object(
            'visible', false, 'is_manager', false, 'code', 'project_missing'
        );
    END IF;
    IF v_project_role IS NULL OR v_project_role NOT IN ('annotator', 'reviewer', 'viewer') THEN
        RETURN pg_catalog.jsonb_build_object(
            'visible', true, 'is_manager', false, 'code', 'invalid_project_role'
        );
    END IF;
    IF v_platform_role = 'viewer' AND v_project_role <> 'viewer' THEN
        RETURN pg_catalog.jsonb_build_object(
            'visible', true, 'is_manager', false, 'code', 'incompatible_project_role'
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'visible', true, 'is_manager', false, 'code', NULL,
        'project_role', v_project_role
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._configuration_project_access(
    jsonb, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._configuration_template_snapshot(
    p_template_id uuid,
    p_lock boolean
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
    v_template jsonb;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_original_hint text;
BEGIN
    IF p_template_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration template snapshot request';
    END IF;
    IF p_lock THEN
        BEGIN
            SELECT pg_catalog.to_jsonb(template)
            INTO v_template
            FROM public.project_templates AS template
            WHERE template.id = p_template_id
            FOR UPDATE OF template NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS
                v_sqlstate = RETURNED_SQLSTATE,
                v_message = MESSAGE_TEXT,
                v_detail = PG_EXCEPTION_DETAIL,
                v_original_hint = PG_EXCEPTION_HINT;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.configuration.template_busy' || CASE
                    WHEN v_original_hint IS NULL OR v_original_hint = '' THEN ''
                    ELSE E'\n' || v_original_hint
                END;
        END;
    ELSE
        SELECT pg_catalog.to_jsonb(template)
        INTO v_template
        FROM public.project_templates AS template
        WHERE template.id = p_template_id;
    END IF;
    RETURN v_template;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._configuration_template_snapshot(
    uuid, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._configuration_template_visible(
    p_actor jsonb,
    p_template jsonb
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope text;
    v_actor_id uuid;
    v_organization_id uuid;
    v_reply jsonb;
BEGIN
    IF p_actor IS NULL OR p_template IS NULL THEN
        RETURN false;
    END IF;
    v_scope := p_template->>'scope';
    v_actor_id := (p_actor->>'id')::uuid;
    IF p_actor->>'role' = 'super_admin' OR v_scope = 'public' THEN
        RETURN true;
    END IF;
    IF v_scope = 'private' THEN
        RETURN p_template->>'created_by' = v_actor_id::text;
    END IF;
    IF v_scope = 'organization'
       AND pg_catalog.jsonb_typeof(p_template->'organization_id') = 'string' THEN
        v_organization_id := (p_template->>'organization_id')::uuid;
        v_reply := aap_identity_v1.workspace_configuration_org_scope(
            v_actor_id, v_organization_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_reply,
            ARRAY['schema', 'actor_id', 'organization_id', 'visible']::text[]
        ) OR v_reply->>'schema' IS DISTINCT FROM
            'aap-identity-workspace-configuration/v1'
          OR pg_catalog.jsonb_typeof(v_reply->'visible') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration organization reply';
        END IF;
        RETURN (v_reply->>'visible')::boolean;
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._configuration_template_visible(
    jsonb, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._step_failure(
    p_request jsonb,
    p_action text,
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
        'schema', 'aap-workspace-configuration/v1',
        'operation_id', p_request->'operation_id',
        'operation', p_request->'operation',
        'action', p_action,
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
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._step_failure(
    jsonb, text, integer, jsonb, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._step_success(
    p_request jsonb,
    p_action text,
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
        'schema', 'aap-workspace-configuration/v1',
        'operation_id', p_request->'operation_id',
        'operation', p_request->'operation',
        'action', p_action,
        'ok', true,
        'facts', p_facts
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._step_success(
    jsonb, text, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._guard_unfinished_attempt()
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
        FROM public.workspace_configuration_operation_attempts AS attempt
        WHERE attempt.transaction_id = NEW.transaction_id
          AND attempt.operation_id = NEW.operation_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Workspace configuration operation was not finished',
            CONSTRAINT = 'workspace_configuration_operation_finished';
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._guard_unfinished_attempt()
    FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER workspace_configuration_operation_finished
AFTER INSERT ON public.workspace_configuration_operation_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
    aap_workspace_configuration_v1._guard_unfinished_attempt();
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1._cleanup_unwritten_state()
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
        FROM public.workspace_configuration_operation_attempts AS attempt
        WHERE attempt.transaction_id = NEW.transaction_id
          AND attempt.operation_id = NEW.operation_id
    ) THEN
        DELETE FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = NEW.transaction_id
          AND state.operation_id = NEW.operation_id;
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1._cleanup_unwritten_state()
    FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER workspace_configuration_state_cleanup
AFTER INSERT ON public.workspace_configuration_operation_states
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
    aap_workspace_configuration_v1._cleanup_unwritten_state();
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1.configuration_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-configuration/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1.configuration_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_configuration_v1.configuration_step(
    p_request jsonb,
    p_action text,
    p_args jsonb,
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
AS $function$
DECLARE
    v_operation text;
    v_action text;
    v_operation_id uuid;
    v_actor_id uuid;
    v_target_id uuid;
    v_selectors jsonb;
    v_request_hash text;
    v_txid bigint;
    v_actor jsonb;
    v_project jsonb;
    v_source_project jsonb;
    v_template jsonb;
    v_project_access jsonb;
    v_source_project_access jsonb;
    v_facts jsonb;
    v_replay jsonb;
    v_receipt public.workspace_configuration_operation_receipts%ROWTYPE;
    v_state public.workspace_configuration_operation_states%ROWTYPE;
    v_state_found boolean;
    v_active boolean;
    v_authorized boolean;
    v_is_creator boolean;
    v_scope text;
    v_organization_id uuid;
    v_id uuid;
    v_resource_id uuid;
    v_display_id text;
    v_registry_id uuid;
    v_pool_id uuid;
    v_project_id uuid;
    v_dataset_id uuid;
    v_item_ids uuid[];
    v_dataset_ids uuid[];
    v_media_ids uuid[];
    v_dataset_reply jsonb;
    v_media_reply jsonb;
    v_backend_reply jsonb;
    v_support_reply jsonb;
    v_summary jsonb;
    v_summaries jsonb;
    v_output jsonb;
    v_result jsonb;
    v_detail jsonb;
    v_headers jsonb;
    v_rows integer;
    v_name text;
    v_value text;
    v_cast text;
    v_table text;
    v_sql text;
    v_columns_sql text;
    v_values_sql text;
    v_assignments_sql text;
    v_column_count integer;
    v_index integer;
    v_action_json jsonb;
    v_source_pool_id uuid;
    v_found boolean;
    v_enabled_pool_id uuid;
    v_authority_actor jsonb;
    v_authority_project jsonb;
    v_authority_source_project jsonb;
    v_authority_template jsonb;
    v_authority_project_access jsonb;
    v_authority_source_access jsonb;
    v_autoflush boolean;
    v_lock_busy boolean;
    v_current_pool_id uuid;
    v_frozen_template jsonb;
    v_frozen_visible boolean;
    v_frozen_editable boolean;
    v_state_columns text[];
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'begin', 'backend', 'source_pool', 'video_annotations',
        'video_trackers', 'video_items', 'video_media', 'task_count',
        'lock_config', 'datasets', 'dataset_scenes', 'authority', 'allocate',
        'insert', 'enable_backend', 'binding', 'update', 'delete', 'finish'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration action';
    END IF;
    v_action := p_action;
    IF NOT aap_workspace_configuration_v1._has_exact_keys(
        p_request,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor_id', 'target_id',
            'selectors', 'payload_ascii'
        ]::text[]
    ) OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
      OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-configuration/v1'
      OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
      OR (p_request->>'operation_id') !~* v_uuid_pattern
      OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
      OR p_request->>'operation' NOT IN (
          'create_project', 'update_project', 'create_template',
          'update_template', 'delete_template', 'duplicate_template'
      )
      OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
      OR (p_request->>'actor_id') !~* v_uuid_pattern
      OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
      OR (
          pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
          AND (p_request->>'target_id') !~* v_uuid_pattern
      )
      OR pg_catalog.jsonb_typeof(p_request->'payload_ascii') IS DISTINCT FROM 'string'
      OR pg_catalog.octet_length(pg_catalog.convert_to(
          p_request->>'payload_ascii', 'UTF8'
      )) <> pg_catalog.char_length(p_request->>'payload_ascii')
      OR NOT aap_workspace_configuration_v1._has_exact_keys(
          p_request->'selectors',
          ARRAY[
              'source_project_id', 'template_id', 'ml_backend_source_id',
              'ml_backend_id', 'ml_backend_present', 'scope', 'scope_present',
              'organization_id', 'organization_present'
          ]::text[]
      ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration request';
    END IF;

    v_operation := p_request->>'operation';
    v_operation_id := (p_request->>'operation_id')::uuid;
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_target_id := CASE
        WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
        THEN (p_request->>'target_id')::uuid
        ELSE NULL
    END;
    v_selectors := p_request->'selectors';
    IF (
        pg_catalog.jsonb_typeof(v_selectors->'source_project_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'source_project_id') = 'string'
            AND (v_selectors->>'source_project_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'template_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'template_id') = 'string'
            AND (v_selectors->>'template_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_source_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'ml_backend_source_id') = 'string'
            AND (v_selectors->>'ml_backend_source_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'ml_backend_id') = 'string'
            AND (v_selectors->>'ml_backend_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_present') IS DISTINCT FROM 'boolean'
        OR pg_catalog.jsonb_typeof(v_selectors->'scope') NOT IN ('null', 'string')
        OR pg_catalog.jsonb_typeof(v_selectors->'scope_present') IS DISTINCT FROM 'boolean'
        OR pg_catalog.jsonb_typeof(v_selectors->'organization_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'organization_id') = 'string'
            AND (v_selectors->>'organization_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'organization_present') IS DISTINCT FROM 'boolean'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration selectors';
    END IF;
    IF (v_operation IN ('create_project', 'create_template') AND v_target_id IS NOT NULL)
       OR (v_operation NOT IN ('create_project', 'create_template') AND v_target_id IS NULL)
       OR (v_operation = 'duplicate_template'
           AND v_target_id IS DISTINCT FROM NULLIF(
               v_selectors->>'template_id', ''
           )::uuid) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration target binding';
    END IF;
    IF p_args IS NULL OR pg_catalog.jsonb_typeof(p_args) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration step arguments';
    END IF;
    IF p_action = 'begin' AND NOT aap_workspace_configuration_v1._has_exact_keys(
        p_args, ARRAY[]::text[]
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid begin arguments';
    ELSIF p_action IN ('backend', 'enable_backend')
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['registry_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend arguments';
    ELSIF p_action = 'source_pool'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['pool_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source-pool arguments';
    ELSIF p_action IN (
        'video_annotations', 'video_trackers', 'video_items', 'task_count',
        'datasets', 'allocate', 'insert', 'delete', 'finish'
    ) AND NOT aap_workspace_configuration_v1._has_exact_keys(
        p_args, ARRAY[]::text[]
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid empty arguments';
    ELSIF p_action = 'video_media'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['item_ids']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video-media arguments';
    ELSIF p_action = 'dataset_scenes'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['dataset_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset-scenes arguments';
    ELSIF p_action = 'lock_config'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['kind']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lock-config arguments';
    ELSIF p_action = 'authority'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['autoflush']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid authority arguments';
    ELSIF p_action = 'binding'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['pool_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid binding arguments';
    ELSIF p_action IN ('insert', 'update')
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY[]::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid write arguments';
    ELSIF p_action IN ('update', 'insert')
       AND (p_column_names IS NULL OR p_column_values IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'missing configuration columns';
    END IF;

    IF p_action IN ('backend', 'enable_backend') THEN
        IF pg_catalog.jsonb_typeof(p_args->'registry_id') IS DISTINCT FROM 'string'
           OR (p_args->>'registry_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend selector';
        END IF;
    ELSIF p_action = 'source_pool' THEN
        IF pg_catalog.jsonb_typeof(p_args->'pool_id') IS DISTINCT FROM 'string'
           OR (p_args->>'pool_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source-pool selector';
        END IF;
    ELSIF p_action = 'dataset_scenes' THEN
        IF pg_catalog.jsonb_typeof(p_args->'dataset_id') IS DISTINCT FROM 'string'
           OR (p_args->>'dataset_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset selector';
        END IF;
    ELSIF p_action = 'lock_config' THEN
        IF pg_catalog.jsonb_typeof(p_args->'kind') IS DISTINCT FROM 'string'
           OR p_args->>'kind' NOT IN ('mask_qc', 'point_cloud') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project config lock kind';
        END IF;
    ELSIF p_action = 'authority' THEN
        IF pg_catalog.jsonb_typeof(p_args->'autoflush') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid authority autoflush';
        END IF;
    ELSIF p_action = 'binding' THEN
        IF pg_catalog.jsonb_typeof(p_args->'pool_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(p_args->'pool_id') = 'string'
               AND (p_args->>'pool_id') !~* v_uuid_pattern) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid binding pool';
        END IF;
    ELSIF p_action = 'video_media' THEN
        IF pg_catalog.jsonb_typeof(p_args->'item_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_args->'item_ids') > 500
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(p_args->'item_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* v_uuid_pattern
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video-media item ids';
        END IF;
    END IF;
    IF p_action = 'update' OR p_action = 'insert' THEN
        v_column_count := pg_catalog.cardinality(p_column_names);
        IF v_column_count IS DISTINCT FROM pg_catalog.cardinality(p_column_values)
           OR pg_catalog.array_position(p_column_names, NULL::text) IS NOT NULL
           OR (
               v_column_count > 0
               AND pg_catalog.array_ndims(p_column_names) IS DISTINCT FROM 1
           ) OR (
               v_column_count > 0
               AND pg_catalog.array_ndims(p_column_values) IS DISTINCT FROM 1
           ) OR EXISTS (
               SELECT 1
               FROM pg_catalog.unnest(p_column_names) AS field(name)
               GROUP BY field.name
               HAVING pg_catalog.count(*) > 1
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid configuration column arrays';
        END IF;
    ELSIF pg_catalog.cardinality(COALESCE(p_column_names, ARRAY[]::text[])) <> 0
       OR pg_catalog.cardinality(COALESCE(p_column_values, ARRAY[]::text[])) <> 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unexpected configuration columns';
    END IF;

    v_request_hash := pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(p_request::text, 'UTF8')),
        'hex'
    );
    v_txid := pg_catalog.txid_current();

    IF p_action = 'begin' THEN
        SELECT state.*
        INTO v_state
        FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        v_state_found := FOUND;
        IF v_state_found THEN
            IF v_state.request_hash IS DISTINCT FROM v_request_hash
               OR v_state.request IS DISTINCT FROM p_request THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 409,
                    pg_catalog.jsonb_build_object('reason', 'operation_conflict')
                );
            END IF;
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_state.facts
            );
        END IF;

        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-workspace-configuration/v1:' || v_operation_id::text, 0
            )
        ) THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 409,
                pg_catalog.jsonb_build_object('reason', 'operation_busy')
            );
        END IF;

        SELECT receipt.*
        INTO v_receipt
        FROM public.workspace_configuration_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.target_id IS DISTINCT FROM v_target_id
               OR v_receipt.request_hash IS DISTINCT FROM v_request_hash
               OR v_receipt.selectors IS DISTINCT FROM v_selectors THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 409,
                    pg_catalog.jsonb_build_object('reason', 'operation_conflict')
                );
            END IF;
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;

            IF v_operation = 'delete_template' THEN
                v_frozen_template := pg_catalog.jsonb_build_object(
                    'scope', v_receipt.frozen_scope,
                    'organization_id', v_receipt.frozen_organization_id,
                    'created_by', v_receipt.frozen_creator_id
                );
                v_frozen_visible := aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_frozen_template
                );
                v_frozen_editable := v_actor->>'role' = 'super_admin'
                    OR v_receipt.frozen_creator_id = v_actor_id;
                IF NOT v_frozen_visible THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF NOT v_frozen_editable THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅模板创建者或超级管理员可删除'::text)
                    );
                END IF;
            ELSIF v_operation IN ('create_project', 'update_project') THEN
                v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    (v_receipt.result->>'resource_id')::uuid,
                    'key_share'
                );
                IF v_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_project_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_project, true
                );
                IF v_project_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                ELSIF COALESCE((v_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                    );
                END IF;
            ELSE
                v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_receipt.result->>'resource_id')::uuid,
                    true
                );
                IF v_template IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF NOT aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_template
                ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF v_actor->>'role' <> 'super_admin'
                   AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅模板创建者或超级管理员可编辑'::text)
                    );
                END IF;
            END IF;
            v_facts := pg_catalog.jsonb_build_object(
                'actor', v_actor,
                'project', v_project,
                'source_project', NULL,
                'template', v_template,
                'replay', v_receipt.result,
                'project_access', v_project_access,
                'source_project_access', NULL,
                'is_manager', CASE
                    WHEN v_project_access IS NULL THEN NULL
                    ELSE v_project_access->'is_manager'
                END
            );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_facts
            );
        END IF;

        IF v_operation = 'create_project' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('需要角色权限: super_admin 或 project_admin'::text)
                );
            END IF;
            IF v_selectors->>'source_project_id' IS NOT NULL THEN
                v_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    (v_selectors->>'source_project_id')::uuid, 'none'
                );
                IF v_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_source_project_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_source_project, false
                );
                IF v_source_project_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
            END IF;
            IF v_selectors->>'template_id' IS NOT NULL THEN
                v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, false
                );
                IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_template
                ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
            END IF;
            v_is_creator := true;
            v_authorized := false;
        ELSIF v_operation = 'update_project' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'none'
            );
            IF v_project IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_project_access := aap_workspace_configuration_v1._configuration_project_access(
                v_actor, v_project, false
            );
            IF v_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE((v_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;
            v_is_creator := true;
            v_authorized := false;
        ELSIF v_operation = 'create_template' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可创建模板'::text)
                );
            END IF;
            v_scope := CASE
                WHEN (v_selectors->>'scope_present')::boolean
                THEN v_selectors->>'scope'
                ELSE 'private'
            END;
            v_organization_id := CASE
                WHEN pg_catalog.jsonb_typeof(v_selectors->'organization_id') = 'string'
                THEN (v_selectors->>'organization_id')::uuid
                ELSE NULL
            END;
            IF v_scope = 'public' AND v_actor->>'role' <> 'super_admin' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅超级管理员可创建公共模板'::text)
                );
            END IF;
            IF v_scope = 'organization' AND v_organization_id IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 400,
                    pg_catalog.to_jsonb('organization scope 必须指定 organization_id'::text)
                );
            END IF;
            v_authorized := false;
            v_is_creator := true;
        ELSE
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_operation = 'duplicate_template'
               AND v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可克隆模板'::text)
                );
            END IF;
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, false
            );
            IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                v_actor, v_template
            ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('模板不存在'::text)
                );
            END IF;
            IF v_operation IN ('update_template', 'delete_template')
               AND v_actor->>'role' <> 'super_admin'
               AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb(
                        CASE WHEN v_operation = 'delete_template'
                            THEN '仅模板创建者或超级管理员可删除'
                            ELSE '仅模板创建者或超级管理员可编辑'
                        END::text
                    )
                );
            END IF;
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, true
            );
            IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                v_actor, v_template
            ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('模板不存在'::text)
                );
            END IF;
            IF v_operation IN ('update_template', 'delete_template')
               AND v_actor->>'role' <> 'super_admin'
               AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb(
                        CASE WHEN v_operation = 'delete_template'
                            THEN '仅模板创建者或超级管理员可删除'
                            ELSE '仅模板创建者或超级管理员可编辑'
                        END::text
                    )
                );
            END IF;
            v_authorized := true;
            v_is_creator := true;
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', v_actor,
            'project', v_project,
            'source_project', v_source_project,
            'template', v_template,
            'replay', NULL,
            'project_access', v_project_access,
            'source_project_access', v_source_project_access,
            'is_manager', COALESCE(v_project_access->'is_manager',
                CASE WHEN v_operation IN ('create_project', 'create_template')
                     OR v_operation = 'duplicate_template' THEN 'true'::jsonb
                     ELSE NULL END)
        );
        INSERT INTO public.workspace_configuration_operation_states (
            transaction_id, operation_id, request_hash, request, facts,
            authorized, source_project_id, target_id, source_backend_id,
            template_id
        ) VALUES (
            v_txid, v_operation_id, v_request_hash, p_request, v_facts,
            v_authorized,
            CASE WHEN v_selectors->>'source_project_id' IS NOT NULL
                 THEN (v_selectors->>'source_project_id')::uuid ELSE NULL END,
            v_target_id,
            CASE WHEN v_selectors->>'ml_backend_source_id' IS NOT NULL
                 THEN (v_selectors->>'ml_backend_source_id')::uuid ELSE NULL END,
            CASE
                WHEN v_operation IN ('update_template', 'delete_template', 'duplicate_template')
                    THEN v_target_id
                WHEN v_selectors->>'template_id' IS NOT NULL
                    THEN (v_selectors->>'template_id')::uuid
                ELSE NULL
            END
        );
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_facts
        );
    END IF;

    SELECT state.*
    INTO v_state
    FROM public.workspace_configuration_operation_states AS state
    WHERE state.transaction_id = v_txid
      AND state.operation_id = v_operation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace configuration operation has not begun';
    END IF;
    IF v_state.request_hash IS DISTINCT FROM v_request_hash
       OR v_state.request IS DISTINCT FROM p_request THEN
        RETURN aap_workspace_configuration_v1._step_failure(
            p_request, p_action, 409,
            pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;

    v_actor := NULLIF(v_state.facts->'actor', 'null'::jsonb);
    v_project := NULLIF(v_state.facts->'project', 'null'::jsonb);
    v_source_project := NULLIF(v_state.facts->'source_project', 'null'::jsonb);
    v_template := NULLIF(v_state.facts->'template', 'null'::jsonb);
    v_authorized := v_state.authorized;
    v_selectors := p_request->'selectors';

    IF p_action = 'authority' THEN
        IF v_operation NOT IN ('create_project', 'update_project', 'create_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'authority not allowed for operation';
        END IF;
        v_autoflush := (p_args->>'autoflush')::boolean;
        IF v_operation = 'create_project'
           AND v_state.source_project_id IS NULL
           AND v_state.binding_pending
           AND v_autoflush IS TRUE THEN
            IF v_state.resource_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace project binding has no inserted project';
            END IF;
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_state.resource_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace project binding target disappeared';
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                INSERT INTO public.workspace_configuration_operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (v_txid, v_operation_id, v_request_hash)
                ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                UPDATE public.projects AS project
                SET ml_backend_pool_id = v_state.primary_pool_id,
                    updated_at = pg_catalog.now()
                WHERE project.id = v_state.resource_id;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Workspace primary backend flush did not update one project';
                END IF;
            END IF;
            UPDATE public.workspace_configuration_operation_states AS state
            SET binding_pending = false
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
            v_state.binding_pending := false;
        END IF;

        v_authority_actor := NULL;
        v_authority_project := NULL;
        v_authority_source_project := NULL;
        v_authority_template := NULL;
        v_authority_project_access := NULL;
        v_authority_source_access := NULL;

        IF v_operation = 'create_project' THEN
            IF v_state.source_project_id IS NOT NULL THEN
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                v_authority_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.source_project_id, 'key_share'
                );
                IF v_authority_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_authority_source_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_authority_actor, v_authority_source_project, true
                );
                IF v_authority_source_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
                IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('需要角色权限: super_admin 或 project_admin'::text)
                    );
                END IF;
            ELSE
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('需要角色权限: super_admin 或 project_admin'::text)
                    );
                END IF;
            END IF;
            IF v_selectors->>'template_id' IS NOT NULL THEN
                v_authority_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, false
                );
                IF v_authority_template IS NULL
                   OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                       v_authority_actor, v_authority_template
                   ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                v_authority_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, true
                );
                IF v_authority_template IS NULL
                   OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                       v_authority_actor, v_authority_template
                   ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
            END IF;
            IF v_state.resource_id IS NOT NULL THEN
                v_authority_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.resource_id, 'none'
                );
            END IF;
        ELSIF v_operation = 'create_template' THEN
            IF v_state.source_project_id IS NOT NULL THEN
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                v_authority_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.source_project_id, 'key_share'
                );
                IF v_authority_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_authority_source_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_authority_actor, v_authority_source_project, true
                );
                IF v_authority_source_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
            ELSE
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
            END IF;
            IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可创建模板'::text)
                );
            END IF;
            v_scope := CASE
                WHEN (v_selectors->>'scope_present')::boolean
                THEN v_selectors->>'scope'
                ELSE 'private'
            END;
            IF v_scope = 'public' AND v_authority_actor->>'role' <> 'super_admin' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅超级管理员可创建公共模板'::text)
                );
            END IF;
        ELSE
            v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_authority_actor IS NULL
               OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            v_authority_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'key_share'
            );
            IF v_authority_project IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_authority_project_access := aap_workspace_configuration_v1._configuration_project_access(
                v_authority_actor, v_authority_project, true
            );
            IF v_authority_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE((v_authority_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', v_authority_actor,
            'project', v_authority_project,
            'source_project', v_authority_source_project,
            'template', v_authority_template,
            'replay', NULL,
            'project_access', v_authority_project_access,
            'source_project_access', v_authority_source_access,
            'is_manager', COALESCE(
                v_authority_project_access->'is_manager',
                CASE WHEN v_operation IN ('create_project', 'create_template')
                     THEN 'true'::jsonb ELSE NULL END
            )
        );
        UPDATE public.workspace_configuration_operation_states AS state
        SET facts = v_facts,
            authorized = true,
            completed_actions = CASE
                WHEN 'authority' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'authority')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_facts
        );
    ELSIF p_action = 'allocate' THEN
        IF v_operation NOT IN ('create_project', 'create_template', 'duplicate_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'allocation not allowed';
        END IF;
        IF NOT v_authorized AND NOT (
            v_operation = 'create_project'
            AND v_state.source_backend_id IS NOT NULL
            AND v_state.source_backend_ready
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration allocation requires authority';
        END IF;
        IF v_state.resource_id IS NOT NULL THEN
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action,
                pg_catalog.jsonb_build_object(
                    'resource_id', v_state.resource_id,
                    'display_id', v_state.display_id
                )
            );
        END IF;
        v_resource_id := pg_catalog.gen_random_uuid();
        IF v_operation = 'create_project' THEN
            v_display_id := 'P-' || pg_catalog.nextval(
                'public.display_seq_projects'::pg_catalog.regclass
            )::text;
        ELSE
            v_display_id := 'PT-' || pg_catalog.nextval(
                'public.display_seq_project_templates'::pg_catalog.regclass
            )::text;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET resource_id = v_resource_id,
            display_id = v_display_id,
            completed_actions = CASE
                WHEN 'allocate' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'allocate')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'resource_id', v_resource_id,
                'display_id', v_display_id
            )
        );
    ELSIF p_action = 'insert' THEN
        IF v_operation NOT IN ('create_project', 'create_template', 'duplicate_template')
           OR v_state.resource_id IS NULL
           OR (
               NOT v_authorized
               AND NOT (
                   v_operation = 'create_project'
                   AND v_state.source_backend_id IS NOT NULL
                   AND v_state.source_backend_ready
               )
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration insert is not admitted';
        END IF;
        INSERT INTO public.workspace_configuration_operation_attempts (
            transaction_id, operation_id, request_hash
        ) VALUES (v_txid, v_operation_id, v_request_hash)
        ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        v_columns_sql := 'id, display_id';
        v_values_sql := pg_catalog.quote_nullable(v_state.resource_id::text) || '::uuid, '
            || pg_catalog.quote_nullable(v_state.display_id) || '::text';
        IF v_operation = 'create_project' THEN
            v_table := 'public.projects';
            v_columns_sql := v_columns_sql || ', owner_id';
            v_values_sql := v_values_sql || ', ' || pg_catalog.quote_nullable(v_actor_id::text) || '::uuid';
        ELSE
            v_table := 'public.project_templates';
            v_columns_sql := v_columns_sql || ', created_by, source_project_id';
            v_values_sql := v_values_sql || ', '
                || pg_catalog.quote_nullable(v_actor_id::text) || '::uuid, '
                || pg_catalog.quote_nullable(CASE
                    WHEN v_operation = 'duplicate_template'
                    THEN v_template->>'source_project_id'
                    WHEN v_state.source_project_id IS NOT NULL
                    THEN v_state.source_project_id::text
                    ELSE NULL
                END) || '::uuid';
        END IF;
        FOR v_index IN 1..COALESCE(v_column_count, 0) LOOP
            v_name := p_column_names[v_index];
            v_value := p_column_values[v_index];
            IF v_operation = 'create_project' THEN
                IF v_name NOT IN (
                    'name', 'type_label', 'type_key', 'data_type', 'tool_bindings',
                    'ai_enabled', 'label_config', 'sampling', 'maximum_annotations',
                    'show_overlap_first', 'iou_dedup_threshold', 'box_threshold',
                    'text_threshold', 'rendering_config', 'annotation_guide',
                    'guide_assets',
                    'due_date', 'ai_interactive_enabled',
                    'raster_mask_native_editing_enabled', 'video_sampling',
                    'video_collaboration', 'scene_mode', 'prefer_same_scene_continuation'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'project insert column is not allowed';
                END IF;
            ELSE
                IF v_name NOT IN (
                    'name', 'description', 'type_label', 'type_key', 'data_type',
                    'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
                    'maximum_annotations', 'show_overlap_first', 'iou_dedup_threshold',
                    'box_threshold', 'text_threshold', 'rendering_config',
                    'annotation_guide', 'scope', 'organization_id'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'template insert column is not allowed';
                END IF;
            END IF;
            IF v_name IN (
                'tool_bindings', 'label_config', 'rendering_config',
                'video_sampling', 'video_collaboration', 'guide_assets',
                'default_variants', 'preannotate_pipeline', 'mask_qc_config',
                'point_cloud_quality_config'
            ) THEN
                v_cast := '::jsonb';
                IF v_value IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'JSON configuration values must use JSON text';
                END IF;
            ELSIF v_name = 'due_date' THEN
                v_cast := '::date';
            ELSIF v_name = 'organization_id' THEN
                v_cast := '::uuid';
            ELSIF v_name IN ('ai_enabled', 'show_overlap_first', 'scene_mode',
                             'prefer_same_scene_continuation',
                             'ai_interactive_enabled',
                             'raster_mask_native_editing_enabled') THEN
                v_cast := '::boolean';
            ELSIF v_name = 'maximum_annotations' THEN
                v_cast := '::integer';
            ELSIF v_name IN ('iou_dedup_threshold', 'box_threshold', 'text_threshold') THEN
                IF v_operation = 'create_project' AND v_name IN ('box_threshold', 'text_threshold') THEN
                    v_cast := '::real';
                ELSE
                    v_cast := '::double precision';
                END IF;
            ELSE
                v_cast := '::text';
            END IF;
            v_columns_sql := v_columns_sql || ', ' || pg_catalog.quote_ident(v_name);
            v_values_sql := v_values_sql || ', '
                || pg_catalog.quote_nullable(v_value) || v_cast;
        END LOOP;
        IF v_operation IN ('create_template', 'duplicate_template')
           AND NOT ('name' = ANY(COALESCE(p_column_names, ARRAY[]::text[]))) THEN
            RAISE EXCEPTION USING ERRCODE = '23502',
                MESSAGE = 'null value in column "name" of relation "project_templates" violates not-null constraint',
                SCHEMA = 'public', TABLE = 'project_templates', COLUMN = 'name';
        END IF;
        v_sql := 'INSERT INTO ' || v_table || ' (' || v_columns_sql || ') VALUES ('
            || v_values_sql || ')';
        EXECUTE v_sql;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'insert' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'insert')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        IF v_operation = 'create_project' THEN
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_state.resource_id, 'none'
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_state.resource_id,
                'project', v_project
            );
        ELSE
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_state.resource_id, false
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_state.resource_id,
                'template', v_template
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    END IF;

    IF p_action = 'enable_backend' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend enablement not allowed';
        END IF;
        v_registry_id := (p_args->>'registry_id')::uuid;
        IF v_registry_id IS DISTINCT FROM NULLIF(v_selectors->>'ml_backend_id', '')::uuid
           AND v_registry_id IS DISTINCT FROM NULLIF(
               v_selectors->>'ml_backend_source_id', ''
           )::uuid
           AND v_registry_id IS DISTINCT FROM v_state.source_backend_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped backend enablement';
        END IF;
        IF v_state.resource_id IS NULL AND v_operation = 'create_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend requires an inserted project';
        END IF;
        IF v_operation = 'create_project'
           AND NOT v_authorized
           AND NOT (
               v_state.source_backend_id = v_registry_id
               AND v_state.source_backend_ready
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'pre-authority backend enablement requires a checked source backend';
        END IF;
        v_project_id := CASE
            WHEN v_operation = 'create_project' THEN v_state.resource_id
            ELSE v_target_id
        END;
        v_backend_reply := aap_inference_v1.enable_workspace_project_backend(
            v_project_id, v_registry_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_backend_reply,
            ARRAY['schema', 'project_id', 'registry_id', 'pool_id']::text[]
        ) OR v_backend_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_backend_reply->>'project_id' IS DISTINCT FROM v_project_id::text
          OR v_backend_reply->>'registry_id' IS DISTINCT FROM v_registry_id::text
          OR pg_catalog.jsonb_typeof(v_backend_reply->'pool_id') NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference enable reply';
        END IF;
        v_enabled_pool_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_backend_reply->'pool_id') = 'string'
            THEN (v_backend_reply->>'pool_id')::uuid
            ELSE NULL
        END;
        IF v_enabled_pool_id IS NOT NULL THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET primary_pool_id = v_enabled_pool_id,
            completed_actions = CASE
                WHEN 'enable_backend' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'enable_backend')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('pool_id', v_backend_reply->'pool_id')
        );
    ELSIF p_action = 'binding' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'primary binding not allowed';
        END IF;
        v_pool_id := CASE
            WHEN pg_catalog.jsonb_typeof(p_args->'pool_id') = 'string'
            THEN (p_args->>'pool_id')::uuid
            ELSE NULL
        END;
        IF v_operation = 'create_project' THEN
            IF v_state.resource_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding requires an inserted project';
            END IF;
            IF v_state.source_backend_id IS NOT NULL
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled source backend';
            END IF;
            IF v_state.source_backend_id IS NULL
               AND v_selectors->>'ml_backend_id' IS NOT NULL
               AND 'enable_backend' = ANY(v_state.completed_actions)
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled backend';
            END IF;
        ELSE
            IF (v_selectors->>'ml_backend_present')::boolean IS DISTINCT FROM true THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'primary binding was not requested';
            END IF;
            IF v_selectors->>'ml_backend_id' IS NULL AND v_pool_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'cleared primary binding must be null';
            END IF;
            IF v_selectors->>'ml_backend_id' IS NOT NULL
               AND 'enable_backend' = ANY(v_state.completed_actions)
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled backend';
            END IF;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET primary_pool_id = v_pool_id,
            binding_pending = true,
            completed_actions = CASE
                WHEN 'binding' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'binding')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, '{}'::jsonb
        );
    ELSIF p_action = 'update' THEN
        IF v_operation NOT IN ('update_project', 'update_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'configuration update not allowed';
        END IF;
        IF v_operation = 'update_project' AND NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project update lacks final authority';
        END IF;
        IF v_operation = 'update_template' AND NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'template update lacks authority';
        END IF;
        v_assignments_sql := '';
        FOR v_index IN 1..COALESCE(v_column_count, 0) LOOP
            v_name := p_column_names[v_index];
            v_value := p_column_values[v_index];
            IF v_operation = 'update_project' THEN
                IF v_name NOT IN (
                    'name', 'type_label', 'type_key', 'data_type', 'status',
                    'tool_bindings', 'ai_enabled', 'due_date', 'sampling',
                    'maximum_annotations', 'show_overlap_first',
                    'iou_dedup_threshold', 'box_threshold', 'text_threshold',
                    'ai_interactive_enabled', 'raster_mask_native_editing_enabled',
                    'mask_qc_config', 'point_cloud_quality_config',
                    'rendering_config', 'annotation_guide', 'guide_assets',
                    'video_sampling', 'video_collaboration', 'scene_mode',
                    'prefer_same_scene_continuation', 'scene_continuation_window_min',
                    'default_variants', 'preannotate_pipeline'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'project update column is not allowed';
                END IF;
            ELSE
                IF v_name NOT IN (
                    'name', 'description', 'type_label', 'type_key', 'data_type',
                    'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
                    'maximum_annotations', 'show_overlap_first',
                    'iou_dedup_threshold', 'box_threshold', 'text_threshold',
                    'rendering_config', 'annotation_guide', 'scope', 'organization_id'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'template update column is not allowed';
                END IF;
            END IF;
            IF v_name IN (
                'tool_bindings', 'label_config', 'rendering_config',
                'video_sampling', 'video_collaboration', 'guide_assets',
                'default_variants', 'preannotate_pipeline', 'mask_qc_config',
                'point_cloud_quality_config'
            ) THEN
                v_cast := '::jsonb';
                IF v_value IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'JSON configuration values must use JSON text';
                END IF;
            ELSIF v_name = 'due_date' THEN
                v_cast := '::date';
            ELSIF v_name = 'organization_id' THEN
                v_cast := '::uuid';
            ELSIF v_name IN (
                'ai_enabled', 'show_overlap_first', 'scene_mode',
                'prefer_same_scene_continuation', 'ai_interactive_enabled',
                'raster_mask_native_editing_enabled'
            ) THEN
                v_cast := '::boolean';
            ELSIF v_name IN ('maximum_annotations', 'scene_continuation_window_min') THEN
                v_cast := '::integer';
            ELSIF v_name IN ('iou_dedup_threshold', 'box_threshold', 'text_threshold') THEN
                IF v_operation = 'update_project'
                   AND v_name IN ('box_threshold', 'text_threshold') THEN
                    v_cast := '::real';
                ELSE
                    v_cast := '::double precision';
                END IF;
            ELSE
                v_cast := '::text';
            END IF;
            IF v_assignments_sql <> '' THEN
                v_assignments_sql := v_assignments_sql || ', ';
            END IF;
            v_assignments_sql := v_assignments_sql
                || pg_catalog.quote_ident(v_name) || ' = '
                || pg_catalog.quote_nullable(v_value) || v_cast;
        END LOOP;

        IF v_operation = 'update_project' AND v_state.binding_pending THEN
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_target_id;
            IF NOT FOUND THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                IF v_assignments_sql <> '' THEN
                    v_assignments_sql := v_assignments_sql || ', ';
                END IF;
                v_assignments_sql := v_assignments_sql
                    || 'ml_backend_pool_id = '
                    || pg_catalog.quote_nullable(v_state.primary_pool_id::text)
                    || '::uuid';
            END IF;
        END IF;

        IF v_assignments_sql <> '' THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            v_table := CASE
                WHEN v_operation = 'update_project' THEN 'public.projects'
                ELSE 'public.project_templates'
            END;
            v_project_id := CASE
                WHEN v_operation = 'update_project' THEN v_target_id
                ELSE v_target_id
            END;
            v_sql := 'UPDATE ' || v_table || ' SET ' || v_assignments_sql
                || ', updated_at = pg_catalog.now() WHERE id = '
                || pg_catalog.quote_nullable(v_project_id::text) || '::uuid';
            EXECUTE v_sql;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                IF v_operation = 'update_template' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
        END IF;
        IF v_operation = 'update_project' AND v_state.binding_pending THEN
            UPDATE public.workspace_configuration_operation_states AS state
            SET binding_pending = false
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'update' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'update')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        IF v_operation = 'update_project' THEN
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'none'
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_target_id,
                'project', v_project
            );
        ELSE
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, false
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_target_id,
                'template', v_template
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    ELSIF p_action = 'delete' THEN
        IF v_operation <> 'delete_template' OR NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'template deletion is not admitted';
        END IF;
        INSERT INTO public.workspace_configuration_operation_attempts (
            transaction_id, operation_id, request_hash
        ) VALUES (v_txid, v_operation_id, v_request_hash)
        ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        DELETE FROM public.project_templates AS template
        WHERE template.id = v_target_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 404,
                pg_catalog.to_jsonb('模板不存在'::text)
            );
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'delete' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'delete')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('resource_id', v_target_id)
        );
    ELSIF p_action = 'finish' THEN
        IF (
            v_operation = 'create_project'
            AND (NOT v_authorized OR NOT ('insert' = ANY(v_state.completed_actions)))
        ) OR (
            v_operation IN ('create_template', 'duplicate_template')
            AND (NOT v_authorized OR NOT ('insert' = ANY(v_state.completed_actions)))
        ) OR (
            v_operation IN ('update_project', 'update_template')
            AND NOT ('update' = ANY(v_state.completed_actions))
        ) OR (
            v_operation = 'delete_template'
            AND NOT ('delete' = ANY(v_state.completed_actions))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration operation is incomplete';
        END IF;

        IF v_operation IN ('create_project', 'update_project')
           AND v_state.binding_pending THEN
            v_project_id := CASE
                WHEN v_operation = 'create_project' THEN v_state.resource_id
                ELSE v_target_id
            END;
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_project_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace configuration binding target disappeared';
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                INSERT INTO public.workspace_configuration_operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (v_txid, v_operation_id, v_request_hash)
                ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                UPDATE public.projects AS project
                SET ml_backend_pool_id = v_state.primary_pool_id,
                    updated_at = pg_catalog.now()
                WHERE project.id = v_project_id;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Workspace configuration binding did not update one project';
                END IF;
            END IF;
        END IF;

        IF v_operation = 'create_project'
           AND v_state.template_id IS NOT NULL THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            UPDATE public.project_templates AS template
            SET usage_count = template.usage_count + 1,
                updated_at = pg_catalog.now()
            WHERE template.id = v_state.template_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace template usage increment did not update one template';
            END IF;
        END IF;

        v_resource_id := CASE
            WHEN v_operation IN ('create_project', 'create_template', 'duplicate_template')
            THEN v_state.resource_id
            ELSE v_target_id
        END;
        v_id := CASE
            WHEN v_operation = 'create_project'
            THEN v_state.template_id
            WHEN v_operation IN ('create_template', 'update_template', 'delete_template', 'duplicate_template')
            THEN v_resource_id
            ELSE NULL
        END;
        v_result := pg_catalog.jsonb_build_object(
            'resource_id', v_resource_id,
            'template_id', v_id
        );
        IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
           OR v_result->>'resource_id' IS DISTINCT FROM v_resource_id::text
           OR (
               v_id IS NULL AND v_result->'template_id' IS DISTINCT FROM 'null'::jsonb
           ) OR (
               v_id IS NOT NULL AND v_result->>'template_id' IS DISTINCT FROM v_id::text
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration finish result';
        END IF;
        INSERT INTO public.workspace_configuration_operation_receipts (
            operation_id, actor_id, operation, target_id, request_hash,
            selectors, result, frozen_scope, frozen_organization_id,
            frozen_creator_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_target_id,
            v_request_hash, v_selectors, v_result,
            CASE WHEN v_operation = 'delete_template' THEN v_template->>'scope' ELSE NULL END,
            CASE WHEN v_operation = 'delete_template'
                 AND pg_catalog.jsonb_typeof(v_template->'organization_id') = 'string'
                 THEN (v_template->>'organization_id')::uuid ELSE NULL END,
            CASE WHEN v_operation = 'delete_template'
                 AND pg_catalog.jsonb_typeof(v_template->'created_by') = 'string'
                 THEN (v_template->>'created_by')::uuid ELSE NULL END
        );
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace configuration receipt was not recorded';
        END IF;
        DELETE FROM public.workspace_configuration_operation_attempts AS attempt
        WHERE attempt.transaction_id = v_txid
          AND attempt.operation_id = v_operation_id;
        DELETE FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_result
        );
    END IF;

    IF p_action = 'backend' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend lookup not allowed';
        END IF;
        v_registry_id := (p_args->>'registry_id')::uuid;
        IF v_registry_id IS DISTINCT FROM NULLIF(v_selectors->>'ml_backend_id', '')::uuid
           AND v_registry_id IS DISTINCT FROM NULLIF(
               v_selectors->>'ml_backend_source_id', ''
           )::uuid
           AND v_registry_id IS DISTINCT FROM v_state.source_backend_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped backend lookup';
        END IF;
        v_backend_reply := aap_inference_v1.workspace_configuration_backend(
            v_registry_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_backend_reply, ARRAY['schema', 'registry_id', 'backend']::text[]
        ) OR v_backend_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_backend_reply->>'registry_id' IS DISTINCT FROM v_registry_id::text
          OR pg_catalog.jsonb_typeof(v_backend_reply->'backend') NOT IN ('null', 'object') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference backend reply';
        END IF;
        IF v_state.source_backend_id = v_registry_id
           AND pg_catalog.jsonb_typeof(v_backend_reply->'backend') = 'object' THEN
            UPDATE public.workspace_configuration_operation_states AS state
            SET source_backend_ready = true,
                completed_actions = CASE
                    WHEN 'backend' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'backend')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        ELSE
            UPDATE public.workspace_configuration_operation_states AS state
            SET completed_actions = CASE
                    WHEN 'backend' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'backend')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('backend', v_backend_reply->'backend')
        );
    ELSIF p_action = 'source_pool' THEN
        IF v_operation <> 'create_project'
           OR v_source_project IS NULL
           OR (v_source_project->>'ml_backend_pool_id') IS DISTINCT FROM p_args->>'pool_id' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped source-pool lookup';
        END IF;
        v_pool_id := (p_args->>'pool_id')::uuid;
        v_support_reply := aap_inference_v1.workspace_configuration_source_pool(
            v_pool_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply,
            ARRAY['schema', 'pool_id', 'legacy_registry_id', 'found']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_support_reply->>'pool_id' IS DISTINCT FROM v_pool_id::text
          OR pg_catalog.jsonb_typeof(v_support_reply->'found') IS DISTINCT FROM 'boolean'
          OR pg_catalog.jsonb_typeof(v_support_reply->'legacy_registry_id') NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference source-pool reply';
        END IF;
        v_registry_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_support_reply->'legacy_registry_id') = 'string'
            THEN (v_support_reply->>'legacy_registry_id')::uuid
            ELSE NULL
        END;
        UPDATE public.workspace_configuration_operation_states AS state
        SET source_backend_id = v_registry_id,
            source_backend_ready = false,
            completed_actions = CASE
                WHEN 'source_pool' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'source_pool')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'found', v_support_reply->'found',
                'legacy_registry_id', v_support_reply->'legacy_registry_id'
            )
        );
    ELSIF p_action IN ('video_annotations', 'video_trackers', 'video_items', 'video_media') THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'video facts require a project update';
        END IF;
        IF p_action = 'video_annotations' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_annotation_count(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'annotation_count']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration annotation facts';
            END IF;
            v_output := pg_catalog.jsonb_build_object(
                'annotation_count', v_support_reply->'annotation_count'
            );
        ELSIF p_action = 'video_trackers' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_tracker_count(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'active_tracker_count']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration tracker facts';
            END IF;
            v_output := pg_catalog.jsonb_build_object(
                'active_tracker_count', v_support_reply->'active_tracker_count'
            );
        ELSIF p_action = 'video_items' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_video_items(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'dataset_item_ids']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text
              OR pg_catalog.jsonb_typeof(v_support_reply->'dataset_item_ids') IS DISTINCT FROM 'array'
              OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.jsonb_array_elements(v_support_reply->'dataset_item_ids') AS item(value)
                  WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                     OR item.value #>> '{}' !~* v_uuid_pattern
              ) THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration video item facts';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(DISTINCT (item.value #>> '{}')::uuid),
                ARRAY[]::uuid[]
            )
            INTO v_media_ids
            FROM pg_catalog.jsonb_array_elements(v_support_reply->'dataset_item_ids') AS item(value);
            UPDATE public.workspace_configuration_operation_states AS state
            SET media_item_ids = v_media_ids,
                completed_actions = CASE
                    WHEN 'video_items' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'video_items')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
            v_output := pg_catalog.jsonb_build_object(
                'dataset_item_ids', v_support_reply->'dataset_item_ids'
            );
        ELSE
            SELECT COALESCE(
                pg_catalog.array_agg(item.value::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_item_ids
            FROM pg_catalog.jsonb_array_elements_text(p_args->'item_ids')
                WITH ORDINALITY AS item(value, ordinality);
            IF NOT v_state.media_item_ids @> v_item_ids THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'video media ids were not read from the selected project';
            END IF;
            v_media_reply := aap_assets_v1.read_media_summaries(v_item_ids);
            IF v_media_reply->>'schema' IS DISTINCT FROM 'aap-assets-media/v1'
               OR v_media_reply->'ok' IS DISTINCT FROM 'true'::jsonb
               OR pg_catalog.jsonb_typeof(v_media_reply->'data') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_media_reply #> '{data,items}') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration Assets media reply';
            END IF;
            v_summaries := '{}'::jsonb;
            FOR v_summary IN
                SELECT item.value
                FROM pg_catalog.jsonb_array_elements(v_media_reply #> '{data,items}') AS item(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_summary->'dataset_item_id') IS DISTINCT FROM 'string'
                   OR (v_summary->>'dataset_item_id') !~* v_uuid_pattern
                   OR NOT v_item_ids @> ARRAY[(v_summary->>'dataset_item_id')::uuid]::uuid[] THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'invalid Workspace configuration Assets media item';
                END IF;
                IF v_summary->>'file_type' = 'video' THEN
                    v_summaries := v_summaries || pg_catalog.jsonb_build_object(
                        v_summary->>'dataset_item_id',
                        CASE
                            WHEN v_summary->>'source_drifted' = 'false'
                             AND v_summary->'source_generation' = v_summary->'materialized_generation'
                            THEN v_summary->'metadata'
                            ELSE 'null'::jsonb
                        END
                    );
                END IF;
            END LOOP;
            v_output := pg_catalog.jsonb_build_object('summaries', v_summaries);
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN p_action = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, p_action)
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    ELSIF p_action = 'task_count' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'task count requires a project update';
        END IF;
        v_support_reply := aap_labeling_v1.workspace_configuration_task_count(
            v_target_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply, ARRAY['schema', 'project_id', 'total_tasks']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-labeling-workspace-configuration/v1'
          OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration task-count facts';
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'task_count' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'task_count')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('total_tasks', v_support_reply->'total_tasks')
        );
    ELSIF p_action = 'datasets' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset read requires a project update';
        END IF;
        v_dataset_reply := aap_assets_v1.workspace_configuration_dataset_rows(
            v_target_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_dataset_reply, ARRAY['schema', 'project_id', 'datasets']::text[]
        ) OR v_dataset_reply->>'schema' IS DISTINCT FROM
            'aap-assets-workspace-configuration/v1'
          OR v_dataset_reply->>'project_id' IS DISTINCT FROM v_target_id::text
          OR pg_catalog.jsonb_typeof(v_dataset_reply->'datasets') IS DISTINCT FROM 'array'
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.jsonb_array_elements(v_dataset_reply->'datasets') AS item(value)
              WHERE NOT aap_workspace_configuration_v1._has_exact_keys(
                  item.value, ARRAY['id', 'display_id', 'data_type']::text[]
              ) OR item.value->>'id' !~* v_uuid_pattern
                OR pg_catalog.jsonb_typeof(item.value->'display_id') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value->'data_type') IS DISTINCT FROM 'string'
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Assets dataset reply';
        END IF;
        SELECT COALESCE(
            pg_catalog.array_agg((item.value->>'id')::uuid ORDER BY item.ordinality),
            ARRAY[]::uuid[]
        )
        INTO v_dataset_ids
        FROM pg_catalog.jsonb_array_elements(v_dataset_reply->'datasets')
            WITH ORDINALITY AS item(value, ordinality);
        UPDATE public.workspace_configuration_operation_states AS state
        SET dataset_ids = v_dataset_ids,
            completed_actions = CASE
                WHEN 'datasets' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'datasets')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('datasets', v_dataset_reply->'datasets')
        );
    ELSIF p_action = 'dataset_scenes' THEN
        v_dataset_id := (p_args->>'dataset_id')::uuid;
        IF v_operation <> 'update_project'
           OR NOT v_state.dataset_ids @> ARRAY[v_dataset_id]::uuid[] THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped dataset scene read';
        END IF;
        v_support_reply := aap_assets_v1.workspace_configuration_dataset_scenes(
            v_dataset_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply, ARRAY['schema', 'dataset_id', 'has_scenes']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-assets-workspace-configuration/v1'
          OR v_support_reply->>'dataset_id' IS DISTINCT FROM v_dataset_id::text
          OR pg_catalog.jsonb_typeof(v_support_reply->'has_scenes') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Assets scene reply';
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'dataset_scenes' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'dataset_scenes')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('has_scenes', v_support_reply->'has_scenes')
        );
    ELSIF p_action = 'lock_config' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project config lock requires update';
        END IF;
        BEGIN
            SELECT project.id INTO v_id
            FROM public.projects AS project
            WHERE project.id = v_target_id
            FOR UPDATE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING
                ERRCODE = SQLSTATE,
                MESSAGE = SQLERRM,
                HINT = 'aap.workspace.configuration.project_busy';
        END;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 404,
                pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;
        v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
            v_target_id, 'none'
        );
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'lock_config' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'lock_config')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'kind', p_args->'kind',
                'project', v_project
            )
        );
    END IF;

    RAISE EXCEPTION USING ERRCODE = '22023',
        MESSAGE = 'unhandled Workspace configuration action';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1.configuration_step(
    jsonb, text, jsonb, text[], text[]
) FROM PUBLIC;
