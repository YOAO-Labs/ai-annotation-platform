-- Fixed Workspace-owned fact phases for the current Project access resolver.
CREATE OR REPLACE FUNCTION aap_workspace_v1.project_access_step(
    p_actor_id uuid,
    p_project_id uuid,
    p_phase text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_facts jsonb;
    v_actor jsonb;
    v_project_row_json text;
    v_member_rows_json text;
    v_sqlstate text;
BEGIN
    IF p_actor_id IS NULL
       OR p_project_id IS NULL
       OR p_phase IS NULL
       OR p_phase NOT IN (
           'lock_actor_share',
           'lock_project_key_share',
           'lock_member_share'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace Project access step';
    END IF;

    IF p_phase = 'lock_actor_share' THEN
        BEGIN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                p_actor_id, true, 'account_busy'
            );
        EXCEPTION WHEN lock_not_available
                     OR serialization_failure
                     OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS v_sqlstate = RETURNED_SQLSTATE;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = 'Workspace Project access lock is busy',
                HINT = 'aap.workspace.project_access.account_busy';
        END;

        IF v_actor IS NOT NULL
           AND (
               pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_actor->'id') IS DISTINCT FROM 'string'
               OR v_actor->>'id' IS DISTINCT FROM p_actor_id::text
               OR pg_catalog.jsonb_typeof(v_actor->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_actor->'is_active') IS DISTINCT FROM 'boolean'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace Project access actor fact';
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', CASE
                WHEN v_actor IS NULL THEN NULL
                ELSE pg_catalog.jsonb_build_object(
                    'actor_id', p_actor_id,
                    'platform_role', v_actor->'role',
                    'is_active', v_actor->'is_active'
                )
            END
        );
    ELSIF p_phase = 'lock_project_key_share' THEN
        BEGIN
            v_project_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    p_project_id, 'key_share'
                );
        EXCEPTION WHEN lock_not_available
                     OR serialization_failure
                     OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS v_sqlstate = RETURNED_SQLSTATE;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = 'Workspace Project access lock is busy',
                HINT = 'aap.workspace.project_access.project_busy';
        END;

        v_facts := pg_catalog.jsonb_build_object(
            'row_json', CASE
                WHEN v_project_row_json IS NULL THEN NULL
                ELSE pg_catalog.to_jsonb(v_project_row_json)
            END
        );
    ELSE
        BEGIN
            v_member_rows_json :=
                aap_workspace_configuration_v1._class_rename_member_rows(
                    p_project_id, p_actor_id
                );
        EXCEPTION WHEN lock_not_available
                     OR serialization_failure
                     OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS v_sqlstate = RETURNED_SQLSTATE;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = 'Workspace Project access lock is busy',
                HINT = 'aap.workspace.project_access.membership_busy';
        END;

        IF v_member_rows_json IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace Project access membership fact';
        END IF;
        v_facts := pg_catalog.jsonb_build_object(
            'rows_json', v_member_rows_json
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-access-step/v1',
        'actor_id', p_actor_id,
        'project_id', p_project_id,
        'phase', p_phase,
        'facts', v_facts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.project_access_step(uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION
    aap_workspace_v1.project_access_step(uuid, uuid, text)
    IS 'Returns one fixed Project access phase fact; caller orders phases and decodes each fact before continuing.';
