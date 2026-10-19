-- Scene-mode image Tasks still expose one immutable image per Task. Scene,
-- cross-frame and point-cloud/camera execution remain outside this capability.
CREATE OR REPLACE FUNCTION aap_assets_v1._accepted_image_input_manifest_current_scope(
    p_actor_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_access jsonb;
    v_code text;
    v_project jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._image_actor(p_actor_id, false);
    IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(
            NULL, COALESCE(v_reply #>> '{error,code}', 'invalid_response')
        );
    END IF;
    v_access := aap_workspace_v1.read_media_project_access(p_actor_id, p_project_id);
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_access #>> '{error,code}';
        IF v_code = 'project_missing' THEN
            v_code := 'task_not_visible';
        ELSIF v_code IN ('project_busy', 'membership_busy') THEN
            v_code := 'source_busy';
        END IF;
        IF v_code IN ('account_not_active', 'account_busy', 'invalid_account_role',
                      'task_not_visible', 'source_busy') THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    SELECT view.project_config INTO v_project
    FROM aap_workspace_v1.project_catalog_configs AS view
    WHERE view.project_id = p_project_id;
    IF NOT FOUND OR pg_catalog.jsonb_typeof(v_project) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'task_not_visible');
    END IF;
    IF v_project ->> 'data_type' IS DISTINCT FROM 'image' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'unsupported_media');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        pg_catalog.jsonb_build_object('actor_id', p_actor_id, 'project_id', p_project_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_current_scope(uuid, uuid)
    FROM PUBLIC;
