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
    IF NOT FOUND OR v_manifest.actor_id <> v_input.actor_id
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
    IF v_manifest.status <> 'preparing' OR v_member.state <> 'preparing' OR v_manifest.prepared_count >= v_manifest.selected_count THEN
        PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    UPDATE public.asset_accepted_video_members SET state=p_state,error_code=p_error,updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=v_input.job_id AND ordinal=v_member.ordinal;
    UPDATE public.asset_accepted_video_manifests SET prepared_count=prepared_count+1 WHERE job_id=v_input.job_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_complete_member(uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_manifest_scope(
    p_manifest public.asset_accepted_video_manifests,p_actor uuid,p_project uuid,p_active boolean
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_code text;
BEGIN
    IF (p_manifest).job_id IS NULL THEN RETURN 'manifest_missing'; END IF;
    v_code := aap_assets_v1._video_input_current_access(p_actor,p_project,NULL,false,true);
    IF v_code IS NOT NULL THEN RETURN v_code; END IF;
    IF (p_manifest).project_id IS DISTINCT FROM p_project THEN RETURN 'manifest_scope_mismatch'; END IF;
    RETURN aap_assets_v1._video_input_code(aap_assets_v1._video_input_job(p_manifest,NULL,p_active),
        'aap-assets-video-input-execution/v1');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_manifest_scope(public.asset_accepted_video_manifests,uuid,uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.capture_accepted_video_manifest(p_job_id uuid,p_actor_id uuid,p_project_id uuid,p_selection jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest public.asset_accepted_video_manifests%ROWTYPE; v_member public.asset_accepted_video_members%ROWTYPE;
    v_item public.dataset_items%ROWTYPE; v_reply jsonb; v_row jsonb; v_task jsonb; v_source jsonb;
    v_code text; v_error text; v_cursor text; v_candidates bigint; v_selected bigint; v_seen bigint := 0;
    v_failed bigint := 0; v_digest text; v_previous uuid;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR NOT aap_assets_v1._video_input_selection_valid(p_selection)
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_selection::text,'UTF8')) > 65536 THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_request'); END IF;
    v_code := aap_assets_v1._video_input_current_access(p_actor_id,p_project_id,NULL,true,true);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended('aap:video-input-manifest:' || p_job_id::text,0)) THEN
        PERFORM aap_assets_v1._video_input_abort('manifest_busy'); END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=p_job_id FOR SHARE NOWAIT;
    IF FOUND THEN
        IF v_manifest.actor_id <> p_actor_id OR v_manifest.project_id <> p_project_id
           OR v_manifest.selection IS DISTINCT FROM p_selection THEN
            PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
        -- Capture can be replayed before Jobs stores the returned selection digest.
        -- Execution and every subsequent operation require the stored digest.
        v_digest := v_manifest.selection_digest;
        v_manifest.selection_digest := NULL;
        v_code := aap_assets_v1._video_input_code(aap_assets_v1._video_input_job(v_manifest,NULL,false),
            'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
        v_manifest.selection_digest := v_digest;
        RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_header(v_manifest),NULL);
    END IF;
    v_manifest.job_id := p_job_id; v_manifest.actor_id := p_actor_id; v_manifest.project_id := p_project_id;
    v_manifest.selection := p_selection;
    v_code := aap_assets_v1._video_input_code(aap_assets_v1._video_input_job(v_manifest,NULL,true),
        'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    v_reply := aap_labeling_v1.open_video_input_manifest_members(p_actor_id,p_project_id,p_selection);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-labeling-video-input-selection/v1');
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    v_cursor := v_reply #>> '{data,cursor}';
    IF NOT aap_assets_v1._video_input_keys(v_reply -> 'data',ARRAY['cursor'])
       OR v_cursor IS NULL OR v_cursor !~ '^aap_video_members_[0-9a-f]{32}$' THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_response'); END IF;
    EXECUTE pg_catalog.format('FETCH NEXT FROM %I',v_cursor) INTO v_row;
    IF v_row ->> 'kind' = 'error' THEN
        v_code := v_row ->> 'code';
        PERFORM aap_assets_v1._video_input_abort(CASE WHEN aap_assets_v1._video_input_error_valid(v_code) THEN v_code ELSE 'invalid_response' END);
    END IF;
    IF NOT aap_assets_v1._video_input_keys(v_row,ARRAY['kind','candidate_count','selected_count'])
       OR v_row ->> 'kind' IS DISTINCT FROM 'header'
       OR NOT aap_assets_v1._video_input_int(v_row -> 'candidate_count',0,9007199254740991)
       OR NOT aap_assets_v1._video_input_int(v_row -> 'selected_count',0,9007199254740991) THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_response'); END IF;
    v_candidates := (v_row ->> 'candidate_count')::bigint; v_selected := (v_row ->> 'selected_count')::bigint;
    IF v_selected > v_candidates OR (p_selection ->> 'predict_mode' <> 'skip_predicted' AND v_selected <> v_candidates)
       OR (p_selection -> 'task_ids' <> 'null'::jsonb AND v_candidates <> pg_catalog.jsonb_array_length(p_selection -> 'task_ids')) THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_response'); END IF;
    v_digest := aap_assets_v1._video_input_selection_seed(p_job_id,p_actor_id,p_project_id,p_selection,v_candidates,v_selected);
    LOOP
        v_row := NULL;
        EXECUTE pg_catalog.format('FETCH NEXT FROM %I',v_cursor) INTO v_row;
        EXIT WHEN v_row IS NULL;
        v_seen := v_seen+1; v_task := v_row -> 'task'; v_source := NULL; v_error := NULL;
        IF v_seen > v_selected OR NOT aap_assets_v1._video_input_keys(v_row,ARRAY['kind','ordinal','task'])
           OR v_row ->> 'kind' IS DISTINCT FROM 'member'
           OR NOT aap_assets_v1._video_input_int(v_row -> 'ordinal',1,9007199254740991)
           OR (v_row ->> 'ordinal')::bigint <> v_seen
           OR NOT aap_assets_v1._video_input_task_valid(v_task,true)
           OR v_task ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR (v_previous IS NOT NULL AND (v_task ->> 'task_id')::uuid <= v_previous)
           OR (p_selection -> 'batch_id' <> 'null'::jsonb AND v_task -> 'batch_id' IS DISTINCT FROM p_selection -> 'batch_id')
           OR (p_selection -> 'task_ids' <> 'null'::jsonb AND NOT (p_selection -> 'task_ids' @> pg_catalog.jsonb_build_array(v_task -> 'task_id'))) THEN
            PERFORM aap_assets_v1._video_input_abort('invalid_response'); END IF;
        v_previous := (v_task ->> 'task_id')::uuid;
        IF v_task -> 'dataset_item_id' = 'null'::jsonb THEN v_error := 'source_missing';
        ELSE
            v_reply := aap_labeling_v1.read_video_input_task_source(v_previous,p_actor_id);
            v_code := aap_assets_v1._video_input_code(v_reply,'aap-labeling-video-input-source/v1');
            IF v_code IN ('source_missing','source_changed','source_not_ready','not_video','task_scope_changed') THEN v_error := v_code;
            ELSIF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code);
            ELSIF ((v_reply #> '{data,binding}') - 'task_version') IS DISTINCT FROM (v_task - ARRAY['batch_id','task_version']) THEN
                v_error := 'task_scope_changed';
            ELSIF v_reply #> '{data,upload_ready}' IS DISTINCT FROM 'true'::jsonb THEN v_error := 'source_not_ready';
            END IF;
            SELECT item.id,item.dataset_id,item.file_name,item.file_path,item.file_type,item.file_size,item.content_hash,
                item.media_source_generation,item.media_source_drifted,item.media_timetable_revision,item.media_object_identity
            INTO v_item.id,v_item.dataset_id,v_item.file_name,v_item.file_path,v_item.file_type,v_item.file_size,v_item.content_hash,
                v_item.media_source_generation,v_item.media_source_drifted,v_item.media_timetable_revision,v_item.media_object_identity
            FROM public.dataset_items AS item WHERE item.id=(v_task ->> 'dataset_item_id')::uuid;
            IF NOT FOUND THEN v_error := COALESCE(v_error,'source_missing');
            ELSIF v_item.file_type IS DISTINCT FROM 'video' THEN v_error := COALESCE(v_error,'not_video');
            ELSE
                v_source := aap_assets_v1._video_input_source_json(v_item);
                IF NOT aap_assets_v1._video_input_source_valid(v_source,true) THEN
                    v_source := NULL; v_error := COALESCE(v_error,'input_corrupt');
                ELSIF v_item.media_source_drifted THEN v_error := COALESCE(v_error,'source_changed'); END IF;
            END IF;
        END IF;
        INSERT INTO public.asset_accepted_video_members(job_id,ordinal,task_id,project_id,batch_id,task,source_facts,capture_error,state,error_code)
        VALUES(p_job_id,v_seen,v_previous,p_project_id,(v_task ->> 'batch_id')::uuid,v_task,v_source,v_error,
            CASE WHEN v_error IS NULL THEN 'unprepared' ELSE 'failed' END,v_error) RETURNING * INTO v_member;
        IF v_error IS NOT NULL THEN v_failed := v_failed+1; END IF;
        v_digest := aap_assets_v1._video_input_capture_digest(v_digest,v_member);
    END LOOP;
    EXECUTE pg_catalog.format('CLOSE %I',v_cursor);
    IF v_seen <> v_selected THEN PERFORM aap_assets_v1._video_input_abort('invalid_response'); END IF;
    INSERT INTO public.asset_accepted_video_manifests(job_id,actor_id,project_id,selection,selection_digest,candidate_count,selected_count,prepared_count)
    VALUES(p_job_id,p_actor_id,p_project_id,p_selection,v_digest,v_candidates,v_selected,v_failed) RETURNING * INTO v_manifest;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_header(v_manifest),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN SQLERRM LIKE 'video_input_abort:%' THEN pg_catalog.substr(SQLERRM,19) ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected OR unique_violation THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'manifest_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_accepted_video_manifest(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_video_manifest(p_job_id uuid,p_actor_id uuid,p_project_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest public.asset_accepted_video_manifests%ROWTYPE; v_code text;
BEGIN
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=p_job_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_manifest_scope(v_manifest,p_actor_id,p_project_id,false);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_header(v_manifest),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_video_manifest(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_video_manifest_page(
    p_job_id uuid,p_actor_id uuid,p_project_id uuid,p_selection_digest text,p_sealed_digest text,p_cursor bigint,p_page_size integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest public.asset_accepted_video_manifests%ROWTYPE; v_code text; v_members jsonb; v_count bigint;
BEGIN
    IF p_cursor IS NULL OR p_cursor < 0 OR p_cursor > 9007199254740991 OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 100
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$'
       OR (p_sealed_digest IS NOT NULL AND p_sealed_digest !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request'); END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=p_job_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_manifest_scope(v_manifest,p_actor_id,p_project_id,false);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    IF v_manifest.selection_digest <> p_selection_digest OR (p_sealed_digest IS NOT NULL
       AND (v_manifest.status <> 'sealed' OR v_manifest.sealed_digest IS DISTINCT FROM p_sealed_digest)) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'manifest_scope_mismatch'); END IF;
    IF p_cursor > v_manifest.selected_count THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_cursor_invalid'); END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(aap_assets_v1._video_input_member(page) ORDER BY page.ordinal),'[]'::jsonb),pg_catalog.count(*)
    INTO v_members,v_count FROM (SELECT member.* FROM public.asset_accepted_video_members AS member
        WHERE member.job_id=p_job_id AND member.ordinal > p_cursor ORDER BY member.ordinal LIMIT p_page_size) AS page;
    IF v_count <> LEAST(p_page_size::bigint,v_manifest.selected_count-p_cursor) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response'); END IF;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('manifest',aap_assets_v1._video_input_header(v_manifest),
        'cursor',p_cursor,'next_cursor',CASE WHEN p_cursor+v_count < v_manifest.selected_count THEN p_cursor+v_count ELSE NULL END,
        'members',v_members),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_video_manifest_page(uuid,uuid,uuid,text,text,bigint,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_accepted_video_member(
    p_job_id uuid,p_actor_id uuid,p_project_id uuid,p_selection_digest text,p_ordinal bigint,p_task_id uuid,
    p_source_snapshot text,p_timetable_revision bigint,p_object_identity jsonb,p_terminal_error text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest public.asset_accepted_video_manifests%ROWTYPE; v_member public.asset_accepted_video_members%ROWTYPE;
    v_input public.asset_accepted_video_inputs%ROWTYPE; v_item public.dataset_items%ROWTYPE;
    v_code text; v_error text; v_reply jsonb; v_source jsonb; v_task jsonb;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL OR p_task_id IS NULL
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$'
       OR p_ordinal IS NULL OR p_ordinal NOT BETWEEN 1 AND 9007199254740991
       OR ((p_object_identity IS NULL) = (p_terminal_error IS NULL))
       OR (p_object_identity IS NOT NULL AND (NOT aap_assets_v1._video_input_identity_valid(p_object_identity)
            OR p_source_snapshot IS NULL OR p_timetable_revision IS NULL))
       OR (p_source_snapshot IS NOT NULL AND p_source_snapshot !~ '^[0-9a-f]{64}$')
       OR (p_timetable_revision IS NOT NULL AND p_timetable_revision NOT BETWEEN 0 AND 9007199254740991)
       OR (p_terminal_error IS NOT NULL AND p_terminal_error NOT IN ('source_missing','source_changed','source_not_ready',
            'not_video','task_scope_changed','timebase_changed','input_expired','input_corrupt','resource_budget_exceeded')) THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_request'); END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=p_job_id FOR UPDATE NOWAIT;
    v_code := aap_assets_v1._video_input_manifest_scope(v_manifest,p_actor_id,p_project_id,false);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF v_manifest.selection_digest <> p_selection_digest THEN PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    SELECT member.* INTO v_member FROM public.asset_accepted_video_members AS member
        WHERE member.job_id=p_job_id AND member.ordinal=p_ordinal FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_member.task_id <> p_task_id THEN PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    -- Once admitted, retries return the original receipt without re-reading a mutable URL.
    IF v_member.state <> 'unprepared' THEN
        RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('manifest',aap_assets_v1._video_input_header(v_manifest),
            'member',aap_assets_v1._video_input_member(v_member)),NULL); END IF;
    IF v_manifest.status <> 'preparing' THEN PERFORM aap_assets_v1._video_input_abort('manifest_not_ready'); END IF;
    v_code := aap_assets_v1._video_input_code(aap_assets_v1._video_input_job(v_manifest,p_task_id,true),
        'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF (p_source_snapshot IS NOT NULL AND p_source_snapshot IS DISTINCT FROM v_member.source_facts ->> 'source_snapshot')
       OR (p_timetable_revision IS NOT NULL AND p_timetable_revision::text IS DISTINCT FROM v_member.source_facts ->> 'timetable_revision') THEN
        PERFORM aap_assets_v1._video_input_abort('input_scope_mismatch'); END IF;
    v_reply := aap_labeling_v1.lock_video_input_manifest_member(p_actor_id,p_project_id,p_task_id,v_manifest.selection,v_member.task);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-labeling-video-input-selection/v1');
    IF v_code IN ('source_missing','source_changed','source_not_ready','not_video','task_scope_changed','timebase_changed') THEN v_error := v_code;
    ELSIF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code);
    ELSIF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_reply #> '{data,upload_ready}' IS DISTINCT FROM 'true'::jsonb
       OR ((v_reply #> '{data,task}') - ARRAY['batch_id','task_version']) IS DISTINCT FROM (v_member.task - ARRAY['batch_id','task_version']) THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_response'); END IF;
    IF v_error IS NULL THEN
        SELECT item.id,item.dataset_id,item.file_name,item.file_path,item.file_type,item.file_size,item.content_hash,
            item.media_source_generation,item.media_source_drifted,item.media_timetable_revision,item.media_object_identity
        INTO v_item.id,v_item.dataset_id,v_item.file_name,v_item.file_path,v_item.file_type,v_item.file_size,v_item.content_hash,
            v_item.media_source_generation,v_item.media_source_drifted,v_item.media_timetable_revision,v_item.media_object_identity
        FROM public.dataset_items AS item WHERE item.id=(v_member.task ->> 'dataset_item_id')::uuid FOR UPDATE NOWAIT;
        IF NOT FOUND THEN v_error := 'source_missing';
        ELSIF v_item.file_type IS DISTINCT FROM 'video' THEN v_error := 'not_video';
        ELSE
            v_source := aap_assets_v1._video_input_source_json(v_item);
            IF NOT aap_assets_v1._video_input_source_valid(v_source,true) THEN v_error := 'input_corrupt';
            ELSIF (v_source - 'object_identity') IS DISTINCT FROM (v_member.source_facts - 'object_identity') THEN
                v_error := CASE WHEN (v_source - ARRAY['object_identity','timetable_revision']) IS NOT DISTINCT FROM
                    (v_member.source_facts - ARRAY['object_identity','timetable_revision']) THEN 'timebase_changed' ELSE 'source_changed' END;
            ELSIF v_member.source_facts -> 'object_identity' <> 'null'::jsonb
               AND v_source -> 'object_identity' IS DISTINCT FROM v_member.source_facts -> 'object_identity' THEN v_error := 'source_changed';
            END IF;
        END IF;
    END IF;
    IF v_error IS NULL AND p_object_identity IS NOT NULL THEN
        IF (p_object_identity ->> 'byte_size')::bigint = 0 THEN v_error := 'source_missing';
        ELSE
            -- Skip reconciliation for a normalized legacy identity that already matches.
            IF v_source -> 'object_identity' IS DISTINCT FROM p_object_identity
               OR (v_item.file_size IS NOT NULL AND v_item.file_size <> (p_object_identity ->> 'byte_size')::bigint) THEN
                v_reply := aap_assets_v1._media_reconcile_source(v_item.id,v_source ->> 'source_snapshot',p_object_identity,NULL);
                v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-media/v1');
                IF v_code IN ('source_missing','source_changed') THEN v_error := v_code;
                ELSIF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code);
                ELSE
                    SELECT item.id,item.dataset_id,item.file_name,item.file_path,item.file_type,item.file_size,item.content_hash,
                        item.media_source_generation,item.media_source_drifted,item.media_timetable_revision,item.media_object_identity
                    INTO v_item.id,v_item.dataset_id,v_item.file_name,v_item.file_path,v_item.file_type,v_item.file_size,v_item.content_hash,
                        v_item.media_source_generation,v_item.media_source_drifted,v_item.media_timetable_revision,v_item.media_object_identity
                    FROM public.dataset_items AS item WHERE item.id=v_item.id;
                    v_source := aap_assets_v1._video_input_source_json(v_item);
                    IF NOT aap_assets_v1._video_input_source_valid(v_source,true) OR v_item.media_source_drifted
                       OR (v_source - 'object_identity') IS DISTINCT FROM (v_member.source_facts - 'object_identity')
                       OR v_source -> 'object_identity' IS DISTINCT FROM p_object_identity THEN v_error := 'source_changed'; END IF;
                END IF;
            END IF;
        END IF;
    END IF;
    v_error := COALESCE(v_error,p_terminal_error);
    IF v_error IS NOT NULL THEN
        UPDATE public.asset_accepted_video_members SET state='failed',error_code=v_error,updated_at=pg_catalog.clock_timestamp()
            WHERE job_id=p_job_id AND ordinal=p_ordinal RETURNING * INTO v_member;
        UPDATE public.asset_accepted_video_manifests SET prepared_count=prepared_count+1 WHERE job_id=p_job_id RETURNING * INTO v_manifest;
    ELSE
        v_task := v_member.task - 'batch_id';
        INSERT INTO public.asset_accepted_video_inputs(job_id,task_id,project_id,actor_id,request_actor_id,task,source,source_facts,sampling)
        VALUES(p_job_id,p_task_id,p_project_id,v_manifest.actor_id,p_actor_id,v_task,
            v_source - ARRAY['source_drifted','file_path','file_type','file_size','content_hash'],v_source,v_manifest.selection -> 'sampling')
            RETURNING * INTO v_input;
        UPDATE public.asset_accepted_video_members SET state='preparing',input_id=v_input.input_id,updated_at=pg_catalog.clock_timestamp()
            WHERE job_id=p_job_id AND ordinal=p_ordinal RETURNING * INTO v_member;
    END IF;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('manifest',aap_assets_v1._video_input_header(v_manifest),
        'member',aap_assets_v1._video_input_member(v_member)),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN SQLERRM LIKE 'video_input_abort:%' THEN pg_catalog.substr(SQLERRM,19) ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected OR unique_violation THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'source_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_accepted_video_member(uuid,uuid,uuid,text,bigint,uuid,text,bigint,jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.seal_accepted_video_manifest(p_job_id uuid,p_actor_id uuid,p_project_id uuid,p_selection_digest text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest public.asset_accepted_video_manifests%ROWTYPE; v_member public.asset_accepted_video_members%ROWTYPE;
    v_code text; v_count bigint; v_ready bigint; v_failed bigint; v_digest text; v_selection text; v_ordinal bigint := 0; v_previous uuid;
BEGIN
    IF p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$' THEN PERFORM aap_assets_v1._video_input_abort('invalid_request'); END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=p_job_id FOR UPDATE NOWAIT;
    v_code := aap_assets_v1._video_input_manifest_scope(v_manifest,p_actor_id,p_project_id,false);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF v_manifest.selection_digest <> p_selection_digest THEN PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    IF v_manifest.status='sealed' THEN RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_header(v_manifest),NULL); END IF;
    v_code := aap_assets_v1._video_input_code(aap_assets_v1._video_input_job(v_manifest,NULL,true),'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    SELECT pg_catalog.count(*),pg_catalog.count(*) FILTER(WHERE member.state='ready'),pg_catalog.count(*) FILTER(WHERE member.state='failed')
        INTO v_count,v_ready,v_failed FROM public.asset_accepted_video_members AS member WHERE member.job_id=p_job_id;
    IF v_manifest.prepared_count <> v_manifest.selected_count OR v_count <> v_manifest.selected_count
       OR v_ready+v_failed <> v_count THEN PERFORM aap_assets_v1._video_input_abort('manifest_not_ready'); END IF;
    v_selection := aap_assets_v1._video_input_selection_seed(p_job_id,v_manifest.actor_id,p_project_id,v_manifest.selection,v_manifest.candidate_count,v_count);
    v_digest := aap_assets_v1._video_input_sealed_seed(v_manifest,v_ready,v_failed);
    FOR v_member IN SELECT member.* FROM public.asset_accepted_video_members AS member WHERE member.job_id=p_job_id ORDER BY member.ordinal LOOP
        v_ordinal := v_ordinal+1;
        IF v_member.ordinal <> v_ordinal OR (v_previous IS NOT NULL AND v_member.task_id <= v_previous)
           OR (v_member.state='ready' AND NOT EXISTS(SELECT 1 FROM public.asset_accepted_video_inputs AS input
                WHERE input.input_id=v_member.input_id AND input.status='ready' AND input.job_id=p_job_id
                  AND input.task_id=v_member.task_id AND input.actor_id=v_manifest.actor_id
                  AND input.task=v_member.task-'batch_id' AND input.sampling=v_manifest.selection -> 'sampling')) THEN
            PERFORM aap_assets_v1._video_input_abort('input_corrupt'); END IF;
        v_previous := v_member.task_id;
        v_selection := aap_assets_v1._video_input_capture_digest(v_selection,v_member);
        v_digest := aap_assets_v1._video_input_outcome_digest(v_digest,v_member);
    END LOOP;
    IF v_selection <> p_selection_digest THEN PERFORM aap_assets_v1._video_input_abort('manifest_scope_mismatch'); END IF;
    UPDATE public.asset_accepted_video_manifests SET status='sealed',ready_count=v_ready,failed_count=v_failed,
        sealed_digest=v_digest,sealed_at=pg_catalog.clock_timestamp() WHERE job_id=p_job_id RETURNING * INTO v_manifest;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_header(v_manifest),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN SQLERRM LIKE 'video_input_abort:%' THEN pg_catalog.substr(SQLERRM,19) ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.seal_accepted_video_manifest(uuid,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_record_scope(
    p_input public.asset_accepted_video_inputs,p_job uuid,p_task uuid,p_actor uuid,p_plan_digest text,p_sealed boolean
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest public.asset_accepted_video_manifests%ROWTYPE; v_code text;
BEGIN
    IF p_job IS NULL OR p_task IS NULL OR p_actor IS NULL OR p_sealed IS NULL THEN RETURN 'invalid_request'; END IF;
    IF (p_input).input_id IS NULL THEN RETURN 'input_missing'; END IF;
    v_code := aap_assets_v1._video_input_current_access(p_actor,(p_input).project_id,(p_input).task,false,false);
    IF v_code IS NOT NULL THEN RETURN v_code; END IF;
    IF (p_input).job_id <> p_job OR (p_input).task_id <> p_task THEN RETURN 'input_scope_mismatch'; END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest WHERE manifest.job_id=p_job;
    IF NOT FOUND OR v_manifest.project_id <> (p_input).project_id OR v_manifest.actor_id <> (p_input).actor_id
       OR NOT EXISTS(SELECT 1 FROM public.asset_accepted_video_members AS member WHERE member.job_id=p_job
            AND member.task_id=p_task AND member.input_id=(p_input).input_id
            AND member.task-'batch_id'=(p_input).task AND member.project_id=(p_input).project_id) THEN RETURN 'input_scope_mismatch'; END IF;
    v_code := aap_assets_v1._video_input_code(aap_assets_v1._video_input_job(v_manifest,p_task,false),'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN v_code; END IF;
    IF p_plan_digest IS NOT NULL THEN
        IF p_plan_digest !~ '^[0-9a-f]{64}$' THEN RETURN 'invalid_request'; END IF;
        IF (p_input).plan IS NULL THEN RETURN 'input_not_ready'; END IF;
        IF (p_input).plan ->> 'plan_digest' IS DISTINCT FROM p_plan_digest THEN RETURN 'input_scope_mismatch'; END IF;
    END IF;
    IF p_sealed AND v_manifest.status <> 'sealed' THEN RETURN 'manifest_not_ready'; END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_record_scope(public.asset_accepted_video_inputs,uuid,uuid,uuid,text,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_video_input(p_input_id uuid,p_job_id uuid,p_task_id uuid,p_actor_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_code text;
BEGIN
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,NULL,false);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_video_input(uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_video_input_work(p_input_id uuid,p_job_id uuid,p_task_id uuid,p_actor_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_code text;
BEGIN
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,NULL,false);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('input',aap_assets_v1._video_input_record(v_input),
        'source',v_input.source_facts),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_video_input_work(uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_video_times(
    p_actor_id uuid,p_job_id uuid,p_task_id uuid,p_input_id uuid,p_plan_digest text,p_cursor bigint,p_page_size integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_code text; v_entries jsonb; v_count bigint; v_total bigint;
BEGIN
    IF p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$' OR p_cursor IS NULL OR p_cursor NOT BETWEEN 0 AND 9007199254740991
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 100 THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request'); END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,p_plan_digest,false);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_total := (v_input.plan ->> 'selected_count')::bigint;
    IF p_cursor > v_total THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_cursor_invalid'); END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(aap_assets_v1._video_input_entry(page.ordinal,page.frame_index,page.pts_ms,page.is_keyframe)
        ORDER BY page.ordinal),'[]'::jsonb),pg_catalog.count(*) INTO v_entries,v_count
    FROM (SELECT entry.* FROM public.asset_accepted_video_times AS entry WHERE entry.input_id=p_input_id
        AND entry.generation=v_input.generation AND entry.lease_token=v_input.last_lease_token AND entry.ordinal > p_cursor
        ORDER BY entry.ordinal LIMIT p_page_size) AS page;
    IF v_count <> LEAST(p_page_size::bigint,v_total-p_cursor) THEN RETURN aap_assets_v1._video_input_reply(NULL,'timebase_incomplete'); END IF;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('plan',v_input.plan,'cursor',p_cursor,
        'next_cursor',CASE WHEN p_cursor+v_count < v_total THEN p_cursor+v_count ELSE NULL END,'entries',v_entries),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_video_times(uuid,uuid,uuid,uuid,text,bigint,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.require_accepted_video_frame(
    p_actor_id uuid,p_job_id uuid,p_task_id uuid,p_input_id uuid,p_plan_digest text,p_ordinal bigint
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_frame public.asset_accepted_video_frames%ROWTYPE;
    v_entry public.asset_accepted_video_times%ROWTYPE; v_code text;
BEGIN
    IF p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$' OR p_ordinal IS NULL OR p_ordinal NOT BETWEEN 1 AND 2147483647 THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_request'); END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,p_plan_digest,true);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF v_input.status='expired' THEN PERFORM aap_assets_v1._video_input_abort('input_expired'); END IF;
    IF v_input.status <> 'ready' THEN PERFORM aap_assets_v1._video_input_abort('input_not_ready'); END IF;
    IF p_ordinal > (v_input.plan ->> 'selected_count')::bigint THEN PERFORM aap_assets_v1._video_input_abort('invalid_frame_range'); END IF;
    v_code := aap_assets_v1._video_input_current_source(v_input,p_actor_id,true,false);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    SELECT entry.* INTO v_entry FROM public.asset_accepted_video_times AS entry WHERE entry.input_id=p_input_id
        AND entry.generation=v_input.generation AND entry.lease_token=v_input.last_lease_token AND entry.ordinal=p_ordinal;
    IF NOT FOUND THEN PERFORM aap_assets_v1._video_input_abort('timebase_incomplete'); END IF;
    SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame WHERE frame.input_id=p_input_id
        AND frame.plan_digest=p_plan_digest AND frame.ordinal=p_ordinal FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        INSERT INTO public.asset_accepted_video_frames(input_id,request_actor_id,plan_digest,ordinal,frame_index,pts_ms,is_keyframe)
        VALUES(p_input_id,p_actor_id,p_plan_digest,p_ordinal,v_entry.frame_index,v_entry.pts_ms,v_entry.is_keyframe) RETURNING * INTO v_frame;
    ELSE
        IF v_frame.frame_index <> v_entry.frame_index OR v_frame.pts_ms <> v_entry.pts_ms OR v_frame.is_keyframe <> v_entry.is_keyframe THEN
            PERFORM aap_assets_v1._video_input_abort('input_corrupt'); END IF;
        IF v_frame.status='expired' THEN PERFORM aap_assets_v1._video_input_abort('input_expired'); END IF;
        IF v_frame.status IN ('pending','failed') THEN
            UPDATE public.asset_accepted_video_frames SET status='pending',error_code=NULL,request_actor_id=p_actor_id,
                updated_at=pg_catalog.clock_timestamp() WHERE frame_id=v_frame.frame_id RETURNING * INTO v_frame;
        END IF;
    END IF;
    UPDATE public.asset_accepted_video_inputs SET retire_after=NULL,retention_checked_at=NULL WHERE input_id=p_input_id;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN SQLERRM LIKE 'video_input_abort:%' THEN pg_catalog.substr(SQLERRM,19) ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected OR unique_violation THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.require_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_video_frame(
    p_actor_id uuid,p_job_id uuid,p_task_id uuid,p_input_id uuid,p_plan_digest text,p_ordinal bigint
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_frame public.asset_accepted_video_frames%ROWTYPE; v_code text;
BEGIN
    IF p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$' OR p_ordinal IS NULL OR p_ordinal NOT BETWEEN 1 AND 2147483647 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request'); END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,p_plan_digest,true);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    IF p_ordinal > (v_input.plan ->> 'selected_count')::bigint THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_frame_range'); END IF;
    SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame WHERE frame.input_id=p_input_id
        AND frame.plan_digest=p_plan_digest AND frame.ordinal=p_ordinal FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_missing'); END IF;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_accepted_video_input(p_input_id uuid,p_job_id uuid,p_task_id uuid,p_actor_id uuid,p_plan_digest text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_code text;
BEGIN
    IF p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$' THEN PERFORM aap_assets_v1._video_input_abort('invalid_request'); END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,p_plan_digest,true);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF v_input.status='expired' THEN PERFORM aap_assets_v1._video_input_abort('input_expired'); END IF;
    IF v_input.status <> 'ready' THEN PERFORM aap_assets_v1._video_input_abort('input_not_ready'); END IF;
    v_code := aap_assets_v1._video_input_current_source(v_input,p_actor_id,true,false);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    UPDATE public.asset_accepted_video_inputs SET retire_after=NULL,retention_checked_at=NULL WHERE input_id=p_input_id RETURNING * INTO v_input;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN SQLERRM LIKE 'video_input_abort:%' THEN pg_catalog.substr(SQLERRM,19) ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_accepted_video_input(uuid,uuid,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_accepted_video_frame(
    p_actor_id uuid,p_job_id uuid,p_task_id uuid,p_input_id uuid,p_plan_digest text,p_ordinal bigint,p_expected_entry jsonb,p_expected_blob jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input public.asset_accepted_video_inputs%ROWTYPE; v_frame public.asset_accepted_video_frames%ROWTYPE; v_code text;
BEGIN
    IF p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$' OR p_ordinal IS NULL OR p_ordinal NOT BETWEEN 1 AND 2147483647
       OR NOT aap_assets_v1._video_input_blob_valid(p_expected_blob,true) OR p_expected_entry IS NULL THEN
        PERFORM aap_assets_v1._video_input_abort('invalid_request'); END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    v_code := aap_assets_v1._video_input_record_scope(v_input,p_job_id,p_task_id,p_actor_id,p_plan_digest,true);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    IF v_input.status='expired' THEN PERFORM aap_assets_v1._video_input_abort('input_expired'); END IF;
    IF v_input.status <> 'ready' THEN PERFORM aap_assets_v1._video_input_abort('input_not_ready'); END IF;
    v_code := aap_assets_v1._video_input_current_source(v_input,p_actor_id,true,false);
    IF v_code IS NOT NULL THEN PERFORM aap_assets_v1._video_input_abort(v_code); END IF;
    SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame WHERE frame.input_id=p_input_id
        AND frame.plan_digest=p_plan_digest AND frame.ordinal=p_ordinal FOR UPDATE NOWAIT;
    IF NOT FOUND THEN PERFORM aap_assets_v1._video_input_abort('frame_missing'); END IF;
    IF v_frame.status='expired' THEN PERFORM aap_assets_v1._video_input_abort('input_expired'); END IF;
    IF v_frame.status <> 'ready' THEN PERFORM aap_assets_v1._video_input_abort('frame_not_ready'); END IF;
    IF aap_assets_v1._video_input_entry(v_frame.ordinal,v_frame.frame_index,v_frame.pts_ms,v_frame.is_keyframe) IS DISTINCT FROM p_expected_entry
       OR v_frame.blob IS DISTINCT FROM p_expected_blob THEN PERFORM aap_assets_v1._video_input_abort('input_scope_mismatch'); END IF;
    UPDATE public.asset_accepted_video_inputs SET retire_after=NULL,retention_checked_at=NULL WHERE input_id=p_input_id;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN SQLERRM LIKE 'video_input_abort:%' THEN pg_catalog.substr(SQLERRM,19) ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint,jsonb,jsonb) FROM PUBLIC;
