-- Cross-owner facts are consumed through the frozen public projections.
CREATE FUNCTION aap_assets_v1._video_input_abort(p_code text)
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='video_input_abort:' || p_code;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_abort(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_code(p_reply jsonb,p_schema text)
RETURNS text LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_code text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_reply) IS DISTINCT FROM 'object'
       OR p_reply ->> 'schema' IS DISTINCT FROM p_schema
       OR pg_catalog.jsonb_typeof(p_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN RETURN 'invalid_response'; END IF;
    IF p_reply -> 'ok' = 'true'::jsonb THEN
        IF NOT aap_assets_v1._video_input_keys(p_reply,ARRAY['schema','ok','data'])
           OR pg_catalog.jsonb_typeof(p_reply -> 'data') IS DISTINCT FROM 'object' THEN RETURN 'invalid_response'; END IF;
        RETURN NULL;
    END IF;
    IF NOT aap_assets_v1._video_input_keys(p_reply,ARRAY['schema','ok','error'])
       OR NOT aap_assets_v1._video_input_keys(p_reply -> 'error',ARRAY['code']) THEN RETURN 'invalid_response'; END IF;
    v_code := p_reply #>> '{error,code}';
    IF v_code = 'project_missing' THEN RETURN 'task_not_visible'; END IF;
    RETURN CASE WHEN aap_assets_v1._video_input_error_valid(v_code) THEN v_code ELSE 'invalid_response' END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_code(jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_current_access(
    p_actor uuid,p_project uuid,p_task jsonb,p_lock boolean,p_manager boolean
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_reply jsonb; v_data jsonb; v_code text;
BEGIN
    IF p_actor IS NULL OR p_project IS NULL OR p_lock IS NULL OR p_manager IS NULL THEN RETURN 'invalid_request'; END IF;
    IF p_lock THEN v_reply := aap_workspace_v1.lock_media_project_access(p_actor,p_project);
    ELSE v_reply := aap_workspace_v1.read_media_project_access(p_actor,p_project); END IF;
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-workspace-media-access/v1');
    IF v_code IS NOT NULL THEN RETURN v_code; END IF;
    v_data := v_reply -> 'data';
    IF NOT aap_assets_v1._video_input_keys(v_data,ARRAY['actor_id','project_id','platform_role','project_role',
        'is_manager','is_super_admin','video_collaboration_enabled','project_name'])
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor::text
       OR v_data ->> 'project_id' IS DISTINCT FROM p_project::text
       OR pg_catalog.jsonb_typeof(v_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN RETURN 'invalid_response'; END IF;
    IF v_data -> 'is_manager' = 'true'::jsonb THEN RETURN NULL; END IF;
    IF p_manager OR p_task IS NULL THEN RETURN 'permission_denied'; END IF;
    v_reply := aap_labeling_v1.read_video_input_task_source((p_task ->> 'task_id')::uuid,p_actor);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-labeling-video-input-source/v1');
    IF v_code IS NOT NULL THEN RETURN v_code; END IF;
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor::text
       OR v_reply #>> '{data,binding,task_id}' IS DISTINCT FROM p_task ->> 'task_id'
       OR v_reply #>> '{data,binding,project_id}' IS DISTINCT FROM p_project::text THEN RETURN 'task_not_visible'; END IF;
    RETURN NULL;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN 'account_busy';
WHEN OTHERS THEN RETURN 'invalid_response';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_current_access(uuid,uuid,jsonb,boolean,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_job(
    p_manifest public.asset_accepted_video_manifests,p_task uuid,p_active boolean
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_reply jsonb; v_code text; v_job jsonb; v_expected_active boolean;
BEGIN
    v_reply := aap_jobs_v1.project_video_input_job((p_manifest).job_id,p_task,(p_manifest).actor_id,
        (p_manifest).project_id,(p_manifest).selection,(p_manifest).selection_digest,p_active);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-jobs-video-input-job/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    IF NOT aap_assets_v1._video_input_keys(v_reply -> 'data',ARRAY['job']) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response'); END IF;
    v_job := v_reply #> '{data,job}';
    IF v_job = 'null'::jsonb THEN
        IF p_active THEN RETURN aap_assets_v1._video_input_reply(NULL,'job_not_active'); END IF;
        RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('job',NULL),NULL);
    END IF;
    IF NOT aap_assets_v1._video_input_keys(v_job,ARRAY['job_id','task_id','actor_id','project_id','retained_project_id',
        'selection','status','cancel_requested','active'])
       OR v_job ->> 'job_id' IS DISTINCT FROM (p_manifest).job_id::text
       OR v_job -> 'task_id' IS DISTINCT FROM COALESCE(pg_catalog.to_jsonb(p_task),'null'::jsonb)
       OR v_job -> 'selection' IS DISTINCT FROM (p_manifest).selection
       OR (v_job -> 'actor_id' <> 'null'::jsonb AND v_job ->> 'actor_id' IS DISTINCT FROM (p_manifest).actor_id::text)
       OR (v_job -> 'project_id' <> 'null'::jsonb AND v_job ->> 'project_id' IS DISTINCT FROM (p_manifest).project_id::text)
       OR (v_job -> 'retained_project_id' <> 'null'::jsonb AND v_job ->> 'retained_project_id' IS DISTINCT FROM (p_manifest).project_id::text)
       OR (v_job -> 'project_id' = 'null'::jsonb AND v_job ->> 'retained_project_id' IS DISTINCT FROM (p_manifest).project_id::text)
       OR pg_catalog.jsonb_typeof(v_job -> 'status') IS DISTINCT FROM 'string'
       OR v_job ->> 'status' NOT IN ('pending','running','completed','failed','cancelled')
       OR pg_catalog.jsonb_typeof(v_job -> 'cancel_requested') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_job -> 'active') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'manifest_scope_mismatch'); END IF;
    v_expected_active := COALESCE(v_job ->> 'actor_id' = (p_manifest).actor_id::text
        AND v_job ->> 'project_id' = (p_manifest).project_id::text
        AND v_job ->> 'status' IN ('pending','running') AND v_job -> 'cancel_requested' = 'false'::jsonb,false);
    IF v_job -> 'active' IS DISTINCT FROM pg_catalog.to_jsonb(v_expected_active)
       OR (p_active AND NOT v_expected_active) THEN RETURN aap_assets_v1._video_input_reply(NULL,'job_not_active'); END IF;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('job',v_job),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'job_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_job(public.asset_accepted_video_manifests,uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_source_json(p_item public.dataset_items)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('dataset_item_id',(p_item).id,'dataset_id',(p_item).dataset_id,
        'source_generation',(p_item).media_source_generation,'source_snapshot',aap_assets_v1._media_source_snapshot(p_item),
        'timetable_revision',(p_item).media_timetable_revision,
        'object_identity',aap_assets_v1._video_input_normalize_identity((p_item).media_object_identity),
        'source_drifted',(p_item).media_source_drifted,'file_path',(p_item).file_path,'file_type',(p_item).file_type,
        'file_size',(p_item).file_size,'content_hash',(p_item).content_hash)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_source_json(public.dataset_items) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_current_source(
    p_input public.asset_accepted_video_inputs,p_actor uuid,p_lock boolean,p_prepare boolean
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_reply jsonb; v_code text; v_task jsonb; v_item public.dataset_items%ROWTYPE;
    v_member public.asset_accepted_video_members%ROWTYPE; v_manifest public.asset_accepted_video_manifests%ROWTYPE;
BEGIN
    IF p_actor IS NULL OR p_lock IS NULL OR p_prepare IS NULL THEN RETURN 'invalid_request'; END IF;
    IF p_prepare THEN
        SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=(p_input).job_id;
        SELECT member.* INTO v_member FROM public.asset_accepted_video_members AS member
            WHERE member.job_id=(p_input).job_id AND member.task_id=(p_input).task_id AND member.input_id=(p_input).input_id;
        IF NOT FOUND THEN RETURN 'input_scope_mismatch'; END IF;
        v_reply := aap_labeling_v1.lock_video_input_manifest_member(p_actor,(p_input).project_id,(p_input).task_id,
            v_manifest.selection,v_member.task);
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-labeling-video-input-selection/v1');
        IF v_code IS NOT NULL THEN RETURN v_code; END IF;
        v_task := (v_reply #> '{data,task}') - 'batch_id';
    ELSE
        IF p_lock THEN v_reply := aap_labeling_v1.lock_video_input_task_source((p_input).task_id,p_actor,(p_input).task);
        ELSE v_reply := aap_labeling_v1.read_video_input_task_source((p_input).task_id,p_actor); END IF;
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-labeling-video-input-source/v1');
        IF v_code IS NOT NULL THEN RETURN v_code; END IF;
        v_task := v_reply #> '{data,binding}';
    END IF;
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor::text
       OR v_reply #> '{data,upload_ready}' IS DISTINCT FROM 'true'::jsonb THEN RETURN 'source_not_ready'; END IF;
    IF NOT aap_assets_v1._video_input_task_valid(v_task,false)
       OR (v_task - 'task_version') IS DISTINCT FROM ((p_input).task - 'task_version') THEN RETURN 'source_changed'; END IF;
    IF p_lock THEN
        SELECT item.id,item.dataset_id,item.file_name,item.file_path,item.file_type,item.file_size,item.content_hash,
            item.media_source_generation,item.media_source_drifted,item.media_timetable_revision,item.media_object_identity
        INTO v_item.id,v_item.dataset_id,v_item.file_name,v_item.file_path,v_item.file_type,v_item.file_size,v_item.content_hash,
            v_item.media_source_generation,v_item.media_source_drifted,v_item.media_timetable_revision,v_item.media_object_identity
        FROM public.dataset_items AS item WHERE item.id=((p_input).source ->> 'dataset_item_id')::uuid FOR SHARE NOWAIT;
    ELSE
        SELECT item.id,item.dataset_id,item.file_name,item.file_path,item.file_type,item.file_size,item.content_hash,
            item.media_source_generation,item.media_source_drifted,item.media_timetable_revision,item.media_object_identity
        INTO v_item.id,v_item.dataset_id,v_item.file_name,v_item.file_path,v_item.file_type,v_item.file_size,v_item.content_hash,
            v_item.media_source_generation,v_item.media_source_drifted,v_item.media_timetable_revision,v_item.media_object_identity
        FROM public.dataset_items AS item WHERE item.id=((p_input).source ->> 'dataset_item_id')::uuid;
    END IF;
    IF NOT FOUND THEN RETURN 'source_missing'; END IF;
    IF v_item.media_source_drifted OR v_item.file_type IS DISTINCT FROM 'video'
       OR v_item.dataset_id::text IS DISTINCT FROM (p_input).source ->> 'dataset_id'
       OR v_item.media_source_generation::text IS DISTINCT FROM (p_input).source ->> 'source_generation'
       OR aap_assets_v1._media_source_snapshot(v_item) IS DISTINCT FROM (p_input).source ->> 'source_snapshot'
       OR aap_assets_v1._video_input_normalize_identity(v_item.media_object_identity)
            IS DISTINCT FROM (p_input).source -> 'object_identity' THEN RETURN 'source_changed'; END IF;
    IF v_item.media_timetable_revision::text IS DISTINCT FROM (p_input).source ->> 'timetable_revision' THEN RETURN 'timebase_changed'; END IF;
    RETURN NULL;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN 'source_busy';
WHEN OTHERS THEN RETURN 'invalid_response';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_current_source(public.asset_accepted_video_inputs,uuid,boolean,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_complete_member(p_input_id uuid,p_state text,p_error text)
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_member public.asset_accepted_video_members%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
BEGIN
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id;
    IF NOT FOUND OR p_state NOT IN ('ready','failed') OR v_input.status IS DISTINCT FROM p_state
       OR v_input.error_code IS DISTINCT FROM p_error THEN PERFORM aap_assets_v1._video_input_abort('input_scope_mismatch'); END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id=v_input.job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_manifest.status <> 'preparing' OR v_manifest.actor_id <> v_input.actor_id
       OR v_manifest.project_id <> v_input.project_id OR v_manifest.selection -> 'sampling' IS DISTINCT FROM v_input.sampling THEN
        PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    SELECT member.* INTO v_member FROM public.asset_accepted_video_members AS member
        WHERE member.job_id=v_input.job_id AND member.task_id=v_input.task_id AND member.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR (v_member.task - 'batch_id') IS DISTINCT FROM v_input.task
       OR v_member.project_id <> v_input.project_id
       OR (v_member.source_facts - 'object_identity') IS DISTINCT FROM (v_input.source_facts - 'object_identity')
       OR (v_member.source_facts -> 'object_identity' <> 'null'::jsonb
           AND v_member.source_facts -> 'object_identity' IS DISTINCT FROM v_input.source -> 'object_identity') THEN
        PERFORM aap_assets_v1._video_input_abort('input_scope_mismatch'); END IF;
    IF v_member.state=p_state AND v_member.error_code IS NOT DISTINCT FROM p_error THEN RETURN; END IF;
    IF v_member.state <> 'preparing' OR v_manifest.prepared_count >= v_manifest.selected_count THEN
        PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    UPDATE public.asset_accepted_video_members SET state=p_state,error_code=p_error,updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=v_input.job_id AND ordinal=v_member.ordinal;
    UPDATE public.asset_accepted_video_manifests SET prepared_count=prepared_count+1 WHERE job_id=v_input.job_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_complete_member(uuid,text,text) FROM PUBLIC;
