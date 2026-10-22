CREATE FUNCTION aap_workspace_v1.write_seed_members(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_operation jsonb;
    v_kind text;
    v_id uuid;
    v_project_id uuid;
    v_user_id uuid;
    v_assigned_by uuid;
    v_role text;
    v_write_role boolean;
    v_write_assigned_by boolean;
    v_assigned_at timestamptz;
    v_updated_at timestamptz;
    v_found boolean;
    v_rows integer;
    v_result jsonb;
    v_results jsonb := '[]'::jsonb;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.jsonb_object_length(p_request) <> 2
       OR NOT (p_request ?& ARRAY['schema', 'operations']::text[])
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-seed-members/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operations') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    FOR v_operation IN
        SELECT operation.value
        FROM pg_catalog.jsonb_array_elements(p_request->'operations')
            WITH ORDINALITY AS operation(value, position)
        ORDER BY operation.position
    LOOP
        IF pg_catalog.jsonb_typeof(v_operation) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;
        IF pg_catalog.jsonb_typeof(v_operation->'kind') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;
        v_kind := v_operation->>'kind';

        IF v_kind = 'insert' THEN
            IF pg_catalog.jsonb_object_length(v_operation) <> 6
               OR NOT (v_operation ?& ARRAY[
                   'kind', 'id', 'project_id', 'user_id', 'role', 'assigned_by'
               ]::text[])
               OR pg_catalog.jsonb_typeof(v_operation->'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'project_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'user_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'assigned_by') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
            END IF;

            v_id := (v_operation->>'id')::uuid;
            v_project_id := (v_operation->>'project_id')::uuid;
            v_user_id := (v_operation->>'user_id')::uuid;
            v_role := v_operation->>'role';
            v_assigned_by := (v_operation->>'assigned_by')::uuid;
            INSERT INTO public.project_members (
                id, project_id, user_id, role, assigned_by
            ) VALUES (
                v_id, v_project_id, v_user_id, v_role, v_assigned_by
            )
            RETURNING assigned_at, updated_at
            INTO v_assigned_at, v_updated_at;

            v_result := pg_catalog.jsonb_build_object(
                'kind', 'insert',
                'id', v_id,
                'assigned_at', v_assigned_at,
                'updated_at', v_updated_at
            );
        ELSIF v_kind = 'update' THEN
            IF pg_catalog.jsonb_object_length(v_operation) <> 6
               OR NOT (v_operation ?& ARRAY[
                   'kind', 'id', 'role', 'assigned_by',
                   'write_role', 'write_assigned_by'
               ]::text[])
               OR pg_catalog.jsonb_typeof(v_operation->'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'assigned_by') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_operation->'write_role') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(v_operation->'write_assigned_by') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
            END IF;

            v_id := (v_operation->>'id')::uuid;
            v_role := v_operation->>'role';
            v_assigned_by := (v_operation->>'assigned_by')::uuid;
            v_write_role := (v_operation->>'write_role')::boolean;
            v_write_assigned_by := (v_operation->>'write_assigned_by')::boolean;
            IF NOT v_write_role AND NOT v_write_assigned_by THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
            END IF;

            v_updated_at := NULL;
            IF v_write_role AND v_write_assigned_by THEN
                UPDATE public.project_members AS member
                SET role = v_role,
                    assigned_by = v_assigned_by,
                    updated_at = pg_catalog.now()
                WHERE member.id = v_id
                RETURNING member.updated_at INTO v_updated_at;
            ELSIF v_write_role THEN
                UPDATE public.project_members AS member
                SET role = v_role,
                    updated_at = pg_catalog.now()
                WHERE member.id = v_id
                RETURNING member.updated_at INTO v_updated_at;
            ELSE
                UPDATE public.project_members AS member
                SET assigned_by = v_assigned_by,
                    updated_at = pg_catalog.now()
                WHERE member.id = v_id
                RETURNING member.updated_at INTO v_updated_at;
            END IF;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows NOT IN (0, 1) THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid seed ProjectMember update row count';
            END IF;
            v_found := v_rows = 1;
            v_result := pg_catalog.jsonb_build_object(
                'kind', 'update',
                'id', v_id,
                'found', v_found,
                'updated_at', v_updated_at
            );
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;

        v_results := v_results || pg_catalog.jsonb_build_array(v_result);
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-seed-members/v1',
        'results', v_results
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.write_seed_members(jsonb)
    FROM PUBLIC;
