-- Retire the pre-managed upload pair. Initialization now records the immutable
-- original allocation before the fixed uploading Task; completion publishes the
-- verified original and casts the Task pointer through the Labeling capability.
--
-- The surviving create capability keeps the same fixed columns and native
-- authority, but admits every closed file type the API infers: image, video and
-- other. The retired pair used to accept arbitrary short type strings; the API
-- only ever derives those three values.

CREATE OR REPLACE FUNCTION aap_labeling_v1.create_uploading_task(
    p_task_id pg_catalog.uuid,
    p_project_id pg_catalog.uuid,
    p_display_id pg_catalog.text,
    p_file_name pg_catalog.text,
    p_file_path pg_catalog.text,
    p_file_type pg_catalog.text,
    p_actor_id pg_catalog.uuid
)
RETURNS pg_catalog.bool
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_authority jsonb;
    v_created boolean := false;
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL
       OR p_display_id IS NULL OR p_display_id !~ '^[A-Za-z0-9-]{1,30}$'
       OR p_file_name IS NULL OR p_file_name = ''
       OR pg_catalog.char_length(p_file_name) > 500
       OR p_file_path IS NULL OR p_file_path = ''
       OR pg_catalog.char_length(p_file_path) > 1000
       OR p_file_type IS NULL
       OR p_file_type NOT IN ('image', 'video', 'other') THEN
        RETURN false;
    END IF;

    -- Recheck current actor/project manager authority and lock the project row
    -- in the caller transaction. Reuses the canonical creation capability.
    v_authority := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_authority IS NULL OR (v_authority ->> 'ok')::boolean IS DISTINCT FROM true THEN
        RETURN false;
    END IF;

    INSERT INTO public.tasks (
        id, project_id, display_id, file_name, file_path, file_type, status
    )
    VALUES (
        p_task_id, p_project_id, p_display_id, p_file_name, p_file_path,
        p_file_type, 'uploading'
    )
    ON CONFLICT (id) DO NOTHING;
    v_created := FOUND;
    RETURN v_created;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.create_uploading_task(
    uuid, uuid, text, text, text, text, uuid) FROM PUBLIC;
-- aap-statement-break
DROP FUNCTION aap_labeling_v1.complete_uploading_task(uuid,uuid,uuid,text,text) RESTRICT;
-- aap-statement-break
DROP FUNCTION aap_labeling_v1.create_uploading_task(uuid,uuid,text,text,text) RESTRICT;
