-- Fixed, read-only Native facts for Workspace ProjectTemplate queries.
CREATE SCHEMA aap_workspace_template_query_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_template_query_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_template_query_v1._has_exact_keys(
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
        ELSE (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(p_value)
        ) = pg_catalog.cardinality(p_keys)
             AND p_value ?& p_keys
             AND NOT EXISTS (
                 SELECT 1
                 FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
                 WHERE NOT (field.name = ANY (p_keys))
             )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_template_query_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_template_query_v1._template_row_json(
    p_template public.project_templates
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', (p_template).id,
        'display_id', (p_template).display_id,
        'name', (p_template).name,
        'description', (p_template).description,
        'type_label', (p_template).type_label,
        'type_key', (p_template).type_key,
        'data_type', (p_template).data_type,
        'tool_bindings', (p_template).tool_bindings,
        'label_config', (p_template).label_config,
        'ai_enabled', (p_template).ai_enabled,
        'sampling', (p_template).sampling,
        'maximum_annotations', (p_template).maximum_annotations,
        'show_overlap_first', (p_template).show_overlap_first,
        'iou_dedup_threshold', (p_template).iou_dedup_threshold::text,
        'box_threshold', (p_template).box_threshold::text,
        'text_threshold', (p_template).text_threshold::text,
        'rendering_config', (p_template).rendering_config,
        'annotation_guide', (p_template).annotation_guide,
        'scope', (p_template).scope,
        'organization_id', (p_template).organization_id,
        'created_by', (p_template).created_by,
        'source_project_id', (p_template).source_project_id,
        'usage_count', (p_template).usage_count,
        'created_at', (p_template).created_at,
        'updated_at', (p_template).updated_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_template_query_v1._template_row_json(
    public.project_templates
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_template_query_v1.template_query_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-template-query/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_template_query_v1.template_query_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_template_query_v1.template_query_step(
    p_request jsonb,
    p_action text,
    p_parameters jsonb,
    p_scope text,
    p_type_keys text[],
    p_search text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_actor_id uuid;
    v_actor_role text;
    v_target_id uuid;
    v_receipt_id uuid;
    v_receipt_operation text;
    v_receipt_actor_id uuid;
    v_receipt_database_operation text;
    v_receipt_found boolean;
    v_creator_id uuid;
    v_organization_ids uuid[];
    v_organization_facts jsonb;
    v_receipt jsonb;
    v_row public.project_templates%ROWTYPE;
    v_row_json jsonb;
    v_rows_json text;
    v_name text;
    v_facts jsonb;
    v_filters_text text;
    v_filters json;
    v_filter_value json;
    v_filter_scope text;
    v_filter_search text;
    v_filter_type_keys text[];
    v_type_kind text;
    v_scope_kind text;
    v_search_kind text;
    v_key_count integer;
    v_distinct_key_count integer;
    v_only_filter_keys boolean;
    v_index integer;
    v_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'organization_ids', 'list_rows', 'get_row', 'projection_row', 'creator_name'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace template query action';
    END IF;
    IF NOT aap_workspace_template_query_v1._has_exact_keys(
        p_request,
        ARRAY[
            'schema', 'request_id', 'operation', 'admitted_actor', 'target_id',
            'configuration_receipt', 'filters_ascii'
        ]::text[]
    ) OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
      OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-template-query/v1'
      OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'string'
      OR (p_request->>'request_id') !~* v_pattern
      OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
      OR p_request->>'operation' NOT IN (
          'list_templates', 'get_template', 'get_template_projection'
      )
      OR NOT aap_workspace_template_query_v1._has_exact_keys(
          p_request->'admitted_actor', ARRAY['id', 'role']::text[]
      )
      OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,id}') IS DISTINCT FROM 'string'
      OR (p_request #>> '{admitted_actor,id}') !~* v_pattern
      OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,role}') IS DISTINCT FROM 'string'
      OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
      OR (
          pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
          AND (p_request->>'target_id') !~* v_pattern
      )
      OR pg_catalog.jsonb_typeof(p_request->'configuration_receipt') NOT IN ('null', 'object')
      OR pg_catalog.jsonb_typeof(p_request->'filters_ascii') NOT IN ('null', 'string') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace template query request';
    END IF;

    v_operation := p_request->>'operation';
    v_actor_id := (p_request #>> '{admitted_actor,id}')::uuid;
    v_actor_role := p_request #>> '{admitted_actor,role}';
    v_target_id := CASE
        WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
        THEN (p_request->>'target_id')::uuid
        ELSE NULL
    END;
    v_filters_text := p_request->>'filters_ascii';

    IF v_operation = 'list_templates' THEN
        IF v_target_id IS NOT NULL
           OR pg_catalog.jsonb_typeof(p_request->'configuration_receipt') <> 'null'
           OR v_filters_text IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace template list request';
        END IF;
        IF pg_catalog.octet_length(pg_catalog.convert_to(v_filters_text, 'UTF8'))
           <> pg_catalog.char_length(v_filters_text) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template filters must be ASCII';
        END IF;
        FOR v_index IN 1..pg_catalog.char_length(v_filters_text) LOOP
            IF pg_catalog.ascii(pg_catalog.substr(v_filters_text, v_index, 1)) < 32 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace template filters contain a raw control';
            END IF;
        END LOOP;
    ELSIF v_operation = 'get_template' THEN
        IF v_target_id IS NULL
           OR pg_catalog.jsonb_typeof(p_request->'configuration_receipt') <> 'null'
           OR v_filters_text IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace template detail request';
        END IF;
    ELSE
        IF v_target_id IS NULL
           OR pg_catalog.jsonb_typeof(p_request->'configuration_receipt') <> 'object'
           OR v_filters_text IS NOT NULL
           OR NOT aap_workspace_template_query_v1._has_exact_keys(
               p_request->'configuration_receipt',
               ARRAY['operation_id', 'operation']::text[]
           )
           OR pg_catalog.jsonb_typeof(
               p_request #> '{configuration_receipt,operation_id}'
           ) IS DISTINCT FROM 'string'
           OR (p_request #>> '{configuration_receipt,operation_id}') !~* v_pattern
           OR pg_catalog.jsonb_typeof(
               p_request #> '{configuration_receipt,operation}'
           ) IS DISTINCT FROM 'string'
           OR p_request #>> '{configuration_receipt,operation}' NOT IN (
               'create_template', 'update_template', 'duplicate_template'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace template projection request';
        END IF;
    END IF;

    IF p_action = 'organization_ids' THEN
        IF v_operation NOT IN ('list_templates', 'get_template')
           OR p_parameters IS NOT NULL
           OR p_scope IS NOT NULL
           OR p_type_keys IS NOT NULL
           OR p_search IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid organization_ids step';
        END IF;
        v_organization_facts := aap_identity_v1.workspace_template_query_org_ids(
            v_actor_id
        );
        IF NOT aap_workspace_template_query_v1._has_exact_keys(
            v_organization_facts,
            ARRAY['schema', 'actor_id', 'organization_ids']::text[]
        ) OR v_organization_facts->>'schema' IS DISTINCT FROM
            'aap-identity-workspace-template-query/v1'
          OR v_organization_facts->>'actor_id' IS DISTINCT FROM v_actor_id::text
          OR pg_catalog.jsonb_typeof(
              v_organization_facts->'organization_ids'
          ) IS DISTINCT FROM 'array'
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.jsonb_array_elements(
                  v_organization_facts->'organization_ids'
              ) AS item(value)
              WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                 OR item.value #>> '{}' !~* v_pattern
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity template-query organization facts';
        END IF;
        v_facts := pg_catalog.jsonb_build_object(
            'organization_ids', v_organization_facts->'organization_ids'
        );
    ELSIF p_action = 'list_rows' THEN
        IF v_operation <> 'list_templates'
           OR NOT aap_workspace_template_query_v1._has_exact_keys(
               p_parameters, ARRAY['organization_ids']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_parameters->'organization_ids')
                IS DISTINCT FROM 'array'
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(p_parameters->'organization_ids')
                   AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* v_pattern
           )
           OR (
               p_type_keys IS NOT NULL
               AND (
                   (pg_catalog.cardinality(p_type_keys) > 0
                    AND pg_catalog.array_ndims(p_type_keys) IS DISTINCT FROM 1)
                   OR pg_catalog.array_position(p_type_keys, NULL::text) IS NOT NULL
               )
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid list_rows parameters';
        END IF;

        BEGIN
            v_filters := v_filters_text::json;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace template list filters';
        END;
        IF pg_catalog.json_typeof(v_filters) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template filters must be an object';
        END IF;
        SELECT pg_catalog.count(*)::integer,
               pg_catalog.count(DISTINCT field.key)::integer,
               pg_catalog.bool_and(field.key IN ('scope', 'type_key', 'search'))
        INTO v_key_count, v_distinct_key_count, v_only_filter_keys
        FROM pg_catalog.json_each(v_filters) AS field(key, value);
        IF v_key_count <> 3 OR v_distinct_key_count <> 3
           OR v_only_filter_keys IS DISTINCT FROM true THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template filters require exactly scope, type_key, and search';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'scope';
        v_scope_kind := pg_catalog.json_typeof(v_filter_value);
        IF v_scope_kind NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template scope filter must be text or null';
        END IF;
        v_filter_scope := pg_catalog.json_extract_path_text(v_filters, 'scope');
        IF v_filter_scope IS DISTINCT FROM p_scope THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template scope filter arguments disagree';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'type_key';
        v_type_kind := pg_catalog.json_typeof(v_filter_value);
        IF v_type_kind = 'null' THEN
            IF p_type_keys IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace template type filter arguments disagree';
            END IF;
            v_filter_type_keys := NULL;
        ELSIF v_type_kind = 'array' THEN
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.json_array_elements(v_filter_value) AS item(value)
                WHERE pg_catalog.json_typeof(item.value) IS DISTINCT FROM 'string'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace template type filter must contain only text';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(item.value ORDER BY item.ordinality),
                ARRAY[]::text[]
            )
            INTO v_filter_type_keys
            FROM pg_catalog.json_array_elements_text(v_filter_value)
                WITH ORDINALITY AS item(value, ordinality);
            IF p_type_keys IS NULL OR v_filter_type_keys IS DISTINCT FROM p_type_keys THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace template type filter arguments disagree';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template type filter must be an array or null';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'search';
        v_search_kind := pg_catalog.json_typeof(v_filter_value);
        IF v_search_kind NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template search filter must be text or null';
        END IF;
        v_filter_search := pg_catalog.json_extract_path_text(v_filters, 'search');
        IF v_filter_search IS DISTINCT FROM p_search THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace template search filter arguments disagree';
        END IF;

        SELECT COALESCE(
            pg_catalog.array_agg(item.value::uuid ORDER BY item.ordinality),
            ARRAY[]::uuid[]
        )
        INTO v_organization_ids
        FROM pg_catalog.jsonb_array_elements_text(
            p_parameters->'organization_ids'
        ) WITH ORDINALITY AS item(value, ordinality);

        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                row_facts.row_json ORDER BY row_facts.created_at DESC
            ),
            '[]'::jsonb
        )::text
        INTO v_rows_json
        FROM (
            SELECT
                template.created_at,
                aap_workspace_template_query_v1._template_row_json(template)
                    AS row_json
            FROM public.project_templates AS template
            WHERE (
                v_actor_role = 'super_admin'
                OR template.scope = 'public'
                OR (
                    template.scope = 'private'
                    AND template.created_by = v_actor_id
                )
                OR (
                    template.scope = 'organization'
                    AND template.organization_id = ANY(v_organization_ids)
                )
            )
              AND (
                  COALESCE(p_scope, '') = ''
                  OR template.scope = p_scope
              )
              AND (
                  COALESCE(pg_catalog.cardinality(p_type_keys), 0) = 0
                  OR template.type_key = ANY(p_type_keys)
              )
              AND (
                  COALESCE(p_search, '') = ''
                  OR template.name ILIKE '%' || p_search || '%'
              )
        ) AS row_facts;
        v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json);
    ELSIF p_action = 'get_row' THEN
        IF v_operation <> 'get_template'
           OR p_parameters IS NOT NULL
           OR p_scope IS NOT NULL
           OR p_type_keys IS NOT NULL
           OR p_search IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid get_row step';
        END IF;
        SELECT template.* INTO v_row
        FROM public.project_templates AS template
        WHERE template.id = v_target_id;
        IF FOUND THEN
            v_row_json := aap_workspace_template_query_v1._template_row_json(v_row);
        ELSE
            v_row_json := NULL;
        END IF;
        v_facts := pg_catalog.jsonb_build_object(
            'row_json', CASE
                WHEN v_row_json IS NULL THEN NULL
                ELSE pg_catalog.to_jsonb(v_row_json::text)
            END
        );
    ELSIF p_action = 'projection_row' THEN
        IF v_operation <> 'get_template_projection'
           OR p_parameters IS NOT NULL
           OR p_scope IS NOT NULL
           OR p_type_keys IS NOT NULL
           OR p_search IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid projection_row step';
        END IF;
        v_receipt_id := (p_request #>> '{configuration_receipt,operation_id}')::uuid;
        v_receipt_operation := p_request #>> '{configuration_receipt,operation}';
        SELECT receipt.result, receipt.actor_id, receipt.operation
        INTO v_receipt, v_receipt_actor_id, v_receipt_database_operation
        FROM public.workspace_configuration_operation_receipts AS receipt
        WHERE receipt.operation_id = v_receipt_id;
        v_receipt_found := FOUND;
        IF NOT v_receipt_found
           OR v_receipt_database_operation IS DISTINCT FROM v_receipt_operation
           OR v_receipt_actor_id IS DISTINCT FROM v_actor_id
           OR pg_catalog.jsonb_typeof(v_receipt) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_receipt->'resource_id') IS DISTINCT FROM 'string'
           OR (v_receipt->>'resource_id') !~* v_pattern
           OR (v_receipt->>'resource_id') IS DISTINCT FROM v_target_id::text THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'configuration receipt does not authorize this projection';
        END IF;
        SELECT template.* INTO v_row
        FROM public.project_templates AS template
        WHERE template.id = v_target_id;
        IF FOUND THEN
            v_row_json := aap_workspace_template_query_v1._template_row_json(v_row);
        ELSE
            v_row_json := NULL;
        END IF;
        v_facts := pg_catalog.jsonb_build_object(
            'row_json', CASE
                WHEN v_row_json IS NULL THEN NULL
                ELSE pg_catalog.to_jsonb(v_row_json::text)
            END
        );
    ELSE
        IF v_operation NOT IN (
            'list_templates', 'get_template', 'get_template_projection'
        )
           OR NOT aap_workspace_template_query_v1._has_exact_keys(
               p_parameters, ARRAY['creator_id']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_parameters->'creator_id') IS DISTINCT FROM 'string'
           OR (p_parameters->>'creator_id') !~* v_pattern
           OR p_scope IS NOT NULL
           OR p_type_keys IS NOT NULL
           OR p_search IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid creator_name step';
        END IF;
        v_creator_id := (p_parameters->>'creator_id')::uuid;
        SELECT account.name
        INTO v_name
        FROM aap_identity_v1.read_workspace_membership_accounts(
            ARRAY[v_creator_id]::uuid[]
        ) AS account
        WHERE account.id = v_creator_id;
        v_facts := pg_catalog.jsonb_build_object(
            'name', CASE
                WHEN v_name IS NULL THEN NULL
                ELSE pg_catalog.to_jsonb(v_name)
            END
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-template-query/v1',
        'request', p_request,
        'action', p_action,
        'facts', v_facts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_template_query_v1.template_query_step(
    jsonb, text, jsonb, text, text[], text
) FROM PUBLIC;
