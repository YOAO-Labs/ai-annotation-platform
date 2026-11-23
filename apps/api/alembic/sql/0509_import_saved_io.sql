CREATE TABLE yoao_assets_v1.import_io_manifests (
    job_id uuid PRIMARY KEY REFERENCES public.dataset_import_operations(job_id) ON DELETE CASCADE,
    lease_token uuid NOT NULL,
    digest bytea NOT NULL CHECK (octet_length(digest) = 32)
);
-- aap-statement-break
CREATE TABLE yoao_assets_v1.import_io_attempts (
    operation_id uuid PRIMARY KEY REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    job_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    entry_index integer NOT NULL,
    publication_id uuid NOT NULL,
    item_id uuid NOT NULL,
    creation_id uuid NOT NULL,
    phase text NOT NULL DEFAULT 'ready' CHECK (phase IN (
        'ready', 'reading', 'read_closed', 'read_complete', 'copying', 'completing', 'written'
    )),
    read_sha256 text CHECK (read_sha256 ~ '^[0-9a-f]{64}$'),
    read_md5 text CHECK (read_md5 ~ '^[0-9a-f]{32}$'),
    CHECK ((phase = 'read_complete') = (read_sha256 IS NOT NULL AND read_md5 IS NOT NULL))
);
-- aap-statement-break
REVOKE ALL ON yoao_assets_v1.import_io_manifests, yoao_assets_v1.import_io_attempts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_io_job(p_job uuid, p_lease uuid, p_stage text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    r public.dataset_import_operations%ROWTYPE;
    proof jsonb;
BEGIN
    IF p_job IS NULL OR p_lease IS NULL THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO r FROM public.dataset_import_operations WHERE job_id = p_job;
    IF NOT FOUND THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'job_missing'); END IF;
    PERFORM 1 FROM public.datasets WHERE id = r.dataset_id FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'dataset_missing'); END IF;
    SELECT * INTO r FROM public.dataset_import_operations WHERE job_id = p_job FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'job_missing'); END IF;
    IF r.stage = 'cancelled' THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'import_cancelled'); END IF;
    IF r.stage IN ('completed', 'failed') THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'job_terminal'); END IF;
    IF r.lease_token IS DISTINCT FROM p_lease OR r.lease_expires_at IS NULL
       OR r.lease_expires_at <= clock_timestamp() THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'lease_lost');
    END IF;
    IF p_stage IS NOT NULL AND r.stage <> p_stage THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    proof := yoao_assets_v1._dataset_import_job_active(p_job, r.dataset_id);
    IF proof #>> '{error,code}' = 'dataset_busy' THEN
        RAISE lock_not_available;
    END IF;
    IF proof -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN proof; END IF;
    proof := yoao_assets_v1._dataset_import_connection_snapshot(p_job);
    IF proof -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, coalesce(proof #>> '{error,code}', 'connection_changed'));
    END IF;
    RETURN yoao_assets_v1._dataset_import_reply(yoao_assets_v1._dataset_import_state(p_job), NULL);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_io_transport(p_job uuid, p_fallback jsonb)
RETURNS jsonb LANGUAGE sql VOLATILE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object('connection_id', c.id, 'revision', c.revision, 'policy', policy.entries)
    FROM public.dataset_import_operations AS j
    JOIN public.storage_connections AS c ON c.id = j.connection_id
    CROSS JOIN LATERAL (SELECT yoao_assets_v1.saved_io_allowlist(p_fallback) AS entries) AS policy
    WHERE j.job_id = p_job AND c.revision >= 1 AND jsonb_typeof(policy.entries) = 'array';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.import_saved_io_list(
    p_job uuid, p_lease uuid, p_fallback jsonb, p_capture jsonb
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    active jsonb := yoao_assets_v1._import_io_job(p_job, p_lease, 'enumerating');
    transport jsonb;
    objects jsonb;
BEGIN
    IF active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN active; END IF;
    transport := yoao_assets_v1._import_io_transport(p_job, p_fallback);
    IF transport IS NULL THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'connection_changed'); END IF;
    IF p_capture IS NOT NULL THEN
        IF p_capture -> 'transport' IS DISTINCT FROM transport THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'connection_changed');
        END IF;
        objects := p_capture -> 'objects';
        IF jsonb_typeof(objects) IS DISTINCT FROM 'array' OR octet_length(objects::text) > 16777216 THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request');
        END IF;
        INSERT INTO yoao_assets_v1.import_io_manifests AS seen (job_id, lease_token, digest)
        VALUES (p_job, p_lease, sha256(convert_to(objects::text, 'UTF8')))
        ON CONFLICT (job_id) DO UPDATE SET lease_token = excluded.lease_token, digest = excluded.digest
        WHERE seen.lease_token <> excluded.lease_token OR seen.digest = excluded.digest;
        IF NOT FOUND THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict'); END IF;
    END IF;
    RETURN yoao_assets_v1._dataset_import_reply(
        jsonb_build_object('job', active -> 'data', 'transport', transport), NULL
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.save_observed_dataset_import_manifest(p_job uuid, p_lease uuid, p_objects jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE active jsonb := yoao_assets_v1._import_io_job(p_job, p_lease, NULL);
BEGIN
    IF active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN active; END IF;
    IF jsonb_typeof(p_objects) IS DISTINCT FROM 'array' OR octet_length(p_objects::text) > 16777216
       OR NOT EXISTS (
           SELECT FROM yoao_assets_v1.import_io_manifests
           WHERE job_id = p_job AND lease_token = p_lease
             AND digest = sha256(convert_to(p_objects::text, 'UTF8'))
       ) THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request'); END IF;
    RETURN yoao_assets_v1.save_dataset_import_manifest(p_job, p_lease, p_objects);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN yoao_assets_v1._dataset_import_reply(NULL, 'dataset_busy');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_io_entry(p_job uuid, p_index bigint, p_publication uuid)
RETURNS public.dataset_import_entries LANGUAGE sql VOLATILE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT e.* FROM public.dataset_import_entries AS e
    JOIN public.dataset_import_operations AS j ON j.job_id = e.job_id
    WHERE e.job_id = p_job AND e.entry_index = p_index AND e.entry_index = j.cursor
      AND e.status = 'pending' AND e.publication_id = p_publication AND e.file_path IS NOT NULL
    FOR UPDATE OF e NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_io_authority(e public.dataset_import_entries)
RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object('kind', 'dataset_import', 'job_id', e.job_id,
        'entry_index', e.entry_index, 'item_id', e.item_id, 'publication_id', e.publication_id);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_dataset_import_original(p_job uuid, p_lease uuid, p_request jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    active jsonb := yoao_assets_v1._import_io_job(p_job, p_lease, 'importing');
    e public.dataset_import_entries%ROWTYPE;
    budget bigint;
BEGIN
    IF active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    e := yoao_assets_v1._import_io_entry(p_job, (p_request #>> '{authority,entry_index}')::bigint,
        (p_request ->> 'publication_id')::uuid);
    IF e.job_id IS NULL
       OR p_request -> 'authority' IS DISTINCT FROM yoao_assets_v1._import_io_authority(e)
       OR p_request ->> 'dataset_id' IS DISTINCT FROM active #>> '{data,dataset_id}'
       OR p_request ->> 'requested_by' IS DISTINCT FROM active #>> '{data,actor_id}'
       OR p_request ->> 'item_id' IS DISTINCT FROM e.item_id::text
       OR p_request ->> 'file_path' IS DISTINCT FROM e.file_path
       OR p_request ->> 'logical_path' IS DISTINCT FROM e.file_name
       OR p_request ->> 'file_type' IS DISTINCT FROM e.file_type
       OR p_request ->> 'kind' IS DISTINCT FROM 'role_media'
       OR p_request ->> 'origin' IS DISTINCT FROM 'connector'
       OR p_request ->> 'mode' IS DISTINCT FROM 'write'
       OR p_request ?| ARRAY['source_contract', 'staging'] THEN
        RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    budget := least((active #>> '{data,settings_snapshot,dataset_import_max_total_bytes}')::bigint
            - (active #>> '{data,committed_bytes}')::bigint, (e.source ->> 'size')::bigint);
    IF (p_request ->> 'max_bytes')::bigint IS NULL OR budget < (e.source ->> 'size')::bigint
       OR (p_request ->> 'max_bytes')::bigint < budget THEN
        RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_byte_budget_exceeded');
    END IF;
    RETURN yoao_assets_v1.begin_saved_operator_original(
        p_request || jsonb_build_object('max_bytes', budget));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN yoao_assets_v1._managed_original_reply(NULL, 'invalid_request');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_io_target(r public.managed_original_creations)
RETURNS jsonb LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE target jsonb;
BEGIN
    PERFORM 1 FROM public.storage_locations
    WHERE id = r.location_id AND connection_id::text IS NOT DISTINCT FROM
        r.configuration #>> '{location,connection_id}' FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF r.configuration #>> '{location,connection_id}' IS NOT NULL THEN
        PERFORM 1 FROM public.storage_connections
        WHERE id = (r.configuration #>> '{location,connection_id}')::uuid FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    target := yoao_assets_v1._media_io_creation_target(r);
    IF target -> 'config_revision' IS DISTINCT FROM r.configuration #> '{original_ref,config_revision}'
       OR target -> 'connection_revision' IS DISTINCT FROM r.configuration -> 'connection_revision'
    THEN RETURN NULL; END IF;
    RETURN target;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.open_dataset_import_original(
    p_job uuid, p_lease uuid, p_index bigint, p_publication uuid, p_creation uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    active jsonb := yoao_assets_v1._import_io_job(p_job, p_lease, 'importing');
    e public.dataset_import_entries%ROWTYPE;
    r public.managed_original_creations%ROWTYPE;
    reply jsonb;
    operation uuid;
BEGIN
    IF active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    e := yoao_assets_v1._import_io_entry(p_job, p_index, p_publication);
    SELECT * INTO r FROM public.managed_original_creations WHERE id = p_creation FOR UPDATE NOWAIT;
    IF NOT FOUND OR e.job_id IS NULL OR r.status <> 'allocated'
       OR r.dataset_id::text IS DISTINCT FROM active #>> '{data,dataset_id}'
       OR r.origin <> 'connector' OR r.owner_kind <> 'dataset'
       OR r.authority IS DISTINCT FROM yoao_assets_v1._import_io_authority(e)
       OR r.requested_by::text IS DISTINCT FROM active #>> '{data,actor_id}'
       OR substr(r.object_key, length(r.configuration #>> '{original_ref,key_prefix}') + 1) IS DISTINCT FROM e.file_path
       OR yoao_assets_v1._import_io_target(r) IS NULL THEN
        RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    reply := yoao_assets_v1.open_operator_storage_operation(p_creation);
    IF reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN yoao_assets_v1._managed_original_reply(NULL, reply #>> '{error,code}');
    END IF;
    operation := (reply #>> '{data,operation_id}')::uuid;
    IF operation IS NULL OR reply #>> '{data,mode}' IS DISTINCT FROM 'write' THEN
        RAISE EXCEPTION 'import write permit missing';
    END IF;
    INSERT INTO yoao_assets_v1.import_io_attempts
        (operation_id, job_id, lease_token, entry_index, publication_id, item_id, creation_id)
    VALUES (operation, p_job, p_lease, e.entry_index, p_publication, e.item_id, p_creation);
    RETURN yoao_assets_v1._managed_original_reply(
        jsonb_build_object('operation_id', operation, 'mode', 'write'), NULL
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_io_attempt(p_scope jsonb, p_live boolean)
RETURNS jsonb LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    j uuid; lease uuid; idx integer; pub uuid; item uuid; creation uuid; op uuid;
    active jsonb;
    e public.dataset_import_entries%ROWTYPE;
    r public.managed_original_creations%ROWTYPE;
    a yoao_assets_v1.import_io_attempts%ROWTYPE;
    target jsonb;
BEGIN
    IF jsonb_typeof(p_scope) IS DISTINCT FROM 'object' OR NOT (p_scope ?& ARRAY[
        'job_id','lease_token','entry_index','publication_id','item_id','creation_id','operation_id'
    ]) OR p_scope - ARRAY[
        'job_id','lease_token','entry_index','publication_id','item_id','creation_id','operation_id'
    ] <> '{}'::jsonb THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request'); END IF;
    j := (p_scope ->> 'job_id')::uuid;
    lease := (p_scope ->> 'lease_token')::uuid;
    idx := (p_scope ->> 'entry_index')::integer;
    pub := (p_scope ->> 'publication_id')::uuid;
    item := (p_scope ->> 'item_id')::uuid;
    creation := (p_scope ->> 'creation_id')::uuid;
    op := (p_scope ->> 'operation_id')::uuid;
    IF p_live THEN
        active := yoao_assets_v1._import_io_job(j, lease, 'importing');
        IF active -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN active; END IF;
        e := yoao_assets_v1._import_io_entry(j, idx, pub);
        IF e.job_id IS NULL OR e.item_id IS DISTINCT FROM item THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
    ELSIF op IS NULL THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    IF op IS NOT NULL THEN
        SELECT * INTO a FROM yoao_assets_v1.import_io_attempts AS attempt
        WHERE attempt.operation_id = op AND attempt.job_id = j AND attempt.lease_token = lease
          AND attempt.entry_index = idx AND attempt.publication_id = pub AND attempt.item_id = item
          AND attempt.creation_id = creation FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict'); END IF;
    END IF;
    SELECT * INTO r FROM public.managed_original_creations WHERE id = creation FOR UPDATE NOWAIT;
    IF NOT FOUND OR r.owner_kind <> 'dataset' OR r.origin <> 'connector'
       OR r.operation_id IS DISTINCT FROM op OR (op IS NULL AND r.status <> 'allocated')
       OR (p_live AND (r.dataset_id::text IS DISTINCT FROM active #>> '{data,dataset_id}'
           OR r.authority IS DISTINCT FROM yoao_assets_v1._import_io_authority(e)
           OR r.requested_by::text IS DISTINCT FROM active #>> '{data,actor_id}'
           OR substr(r.object_key, length(r.configuration #>> '{original_ref,key_prefix}') + 1) IS DISTINCT FROM e.file_path)) THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    IF p_live AND op IS NOT NULL THEN
        IF NOT yoao_assets_v1._media_io_permit(op, r.location_id, 'managed_original_creation')
           OR NOT EXISTS (
               SELECT FROM public.storage_operations
               WHERE id = op AND revision = r.maintenance_revision
           ) THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'storage_maintenance');
        END IF;
        target := yoao_assets_v1._import_io_target(r);
        IF target IS NULL THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'connection_changed'); END IF;
    END IF;
    RETURN yoao_assets_v1._dataset_import_reply(jsonb_build_object(
        'source', e.source, 'path', active #> '{data,source_path}', 'target', target,
        'registered', r.location_id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid,
        'status', r.status, 'phase', a.phase, 'budget', r.max_bytes,
        'timeout', r.timeout_seconds, 'content_type', r.content_type,
        'read_sha256', a.read_sha256, 'read_md5', a.read_md5
    ), NULL);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.import_saved_io_action(
    p_scope jsonb, p_step text, p_fallback jsonb, p_payload jsonb
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    proof jsonb;
    facts jsonb;
    transport jsonb;
    phase text;
    creation uuid;
    op uuid;
    reply jsonb;
    media jsonb;
    evidence jsonb;
    code text;
BEGIN
    IF p_step IS NULL OR p_step NOT IN ('read_start', 'read_check', 'read_closed', 'read_complete',
        'copy_start', 'copy_check', 'copy_prepare', 'copy_written', 'copy_failed') THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
    proof := yoao_assets_v1._import_io_attempt(p_scope, p_step NOT IN ('read_closed', 'copy_failed'));
    IF proof -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN proof; END IF;
    facts := proof -> 'data';
    phase := facts ->> 'phase';
    creation := (p_scope ->> 'creation_id')::uuid;
    op := (p_scope ->> 'operation_id')::uuid;
    IF phase IS NULL OR (facts ->> 'registered')::boolean IS DISTINCT FROM starts_with(p_step, 'copy_') THEN
        RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    IF p_step NOT IN ('read_closed', 'copy_failed') THEN
        transport := yoao_assets_v1._import_io_transport((p_scope ->> 'job_id')::uuid, p_fallback);
        IF transport IS NULL OR (p_step NOT IN ('read_start', 'copy_start') AND (
            p_payload -> 'transport' IS DISTINCT FROM transport
            OR p_payload -> 'target' IS DISTINCT FROM facts -> 'target'
            OR p_payload -> 'source' IS DISTINCT FROM facts -> 'source'
        )) THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'connection_changed'); END IF;
    END IF;
    CASE p_step
    WHEN 'read_start', 'copy_start' THEN
        IF phase <> 'ready' OR facts ->> 'status' <> 'preparing'
           OR (facts #>> '{source,size}')::bigint > (facts ->> 'budget')::bigint THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        IF p_step = 'copy_start' THEN
            media := yoao_assets_v1.media_io_creation(creation, op, NULL, 'stream', true);
            IF media IS NULL THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict'); END IF;
        END IF;
        UPDATE yoao_assets_v1.import_io_attempts SET phase = CASE p_step
            WHEN 'read_start' THEN 'reading' ELSE 'copying' END WHERE operation_id = op;
    WHEN 'read_check', 'copy_check' THEN
        IF (p_step = 'read_check' AND phase <> 'reading')
           OR (p_step = 'copy_check' AND phase NOT IN ('copying', 'completing')) THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
    WHEN 'read_closed' THEN
        IF phase <> 'reading' THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict'); END IF;
        UPDATE yoao_assets_v1.import_io_attempts SET phase = 'read_closed' WHERE operation_id = op;
    WHEN 'read_complete' THEN
        evidence := p_payload -> 'evidence';
        IF phase <> 'read_closed'
           OR (evidence ->> 'sha256' ~ '^[0-9a-f]{64}$') IS DISTINCT FROM true
           OR (evidence ->> 'md5' ~ '^[0-9a-f]{32}$') IS DISTINCT FROM true THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        UPDATE yoao_assets_v1.import_io_attempts
        SET phase = 'read_complete', read_sha256 = evidence ->> 'sha256', read_md5 = evidence ->> 'md5'
        WHERE operation_id = op;
    WHEN 'copy_prepare' THEN
        evidence := p_payload -> 'evidence';
        IF phase <> 'copying' OR evidence ->> 'size_bytes' IS DISTINCT FROM facts #>> '{source,size}'
           OR NOT yoao_assets_v1.media_io_creation_evidence(creation, op, 'prepare', evidence) THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        reply := yoao_assets_v1.record_operator_original_prepared(
            creation, (evidence - ARRAY['upload_id', 'parts']) || '{"source_owned":true}'::jsonb);
        IF reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'import prepare evidence rejected'; END IF;
        reply := yoao_assets_v1.mark_operator_original_writing(creation);
        IF reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'import writing admission rejected'; END IF;
        media := yoao_assets_v1.media_io_creation(creation, op, NULL, 'complete', true);
        IF media IS NULL THEN RAISE EXCEPTION 'import completion admission rejected'; END IF;
        UPDATE yoao_assets_v1.import_io_attempts SET phase = 'completing' WHERE operation_id = op;
    WHEN 'copy_written' THEN
        evidence := p_payload -> 'evidence';
        IF phase <> 'completing' OR NOT yoao_assets_v1.media_io_creation_evidence(creation, op, 'complete', evidence) THEN
            RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
        END IF;
        reply := yoao_assets_v1.record_operator_original_written(creation, evidence);
        IF reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'import write receipt rejected'; END IF;
        UPDATE yoao_assets_v1.import_io_attempts SET phase = 'written' WHERE operation_id = op;
    WHEN 'copy_failed' THEN
        code := p_payload ->> 'code';
        IF phase NOT IN ('copying','completing') OR code IS NULL OR code NOT IN (
            'source_changed', 'source_not_found', 'source_read_failed', 'invalid_source_object', 'destination_write_failed'
        ) THEN RETURN yoao_assets_v1._dataset_import_reply(NULL, 'operation_conflict'); END IF;
        reply := yoao_assets_v1.fail_operator_original(creation, 'failed', code,
            '{"physical_settled":true,"physical_outcome":"not_written","stage":"write"}'::jsonb);
        IF reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'import failed copy not settled'; END IF;
    END CASE;
    RETURN yoao_assets_v1._dataset_import_reply(facts || jsonb_build_object('transport', transport), NULL);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._import_builtin_original(p_scope jsonb, p_phase text, p_prepared jsonb)
RETURNS uuid LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE proof jsonb := yoao_assets_v1._import_io_attempt(p_scope, true) -> 'data';
BEGIN
    IF proof -> 'registered' IS DISTINCT FROM 'false'::jsonb OR (
        p_phase = 'fail' AND coalesce(proof ->> 'phase', 'ready') NOT IN ('ready', 'read_closed', 'read_complete')
        OR p_phase <> 'fail' AND (proof ->> 'phase' IS DISTINCT FROM 'read_complete'
            OR proof ->> 'status' IS DISTINCT FROM p_phase)
    ) OR (p_phase = 'preparing' AND (
        p_prepared -> 'size_bytes' IS DISTINCT FROM proof #> '{source,size}'
        OR p_prepared -> 'sha256' IS DISTINCT FROM proof -> 'read_sha256'
        OR p_prepared -> 'md5' IS DISTINCT FROM proof -> 'read_md5'
    )) THEN RETURN NULL; END IF;
    RETURN (p_scope ->> 'creation_id')::uuid;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_import_original_prepared(p_scope jsonb, p_prepared jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE id uuid := yoao_assets_v1._import_builtin_original(p_scope, 'preparing', p_prepared);
BEGIN
    IF id IS NULL THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch'); END IF;
    RETURN yoao_assets_v1.record_operator_original_prepared(id, p_prepared);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.mark_import_original_writing(p_scope jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE id uuid := yoao_assets_v1._import_builtin_original(p_scope, 'prepared', NULL);
BEGIN
    IF id IS NULL THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch'); END IF;
    RETURN yoao_assets_v1.mark_operator_original_writing(id);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_import_original_written(p_scope jsonb, p_receipt jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE id uuid := yoao_assets_v1._import_builtin_original(p_scope, 'writing', NULL);
BEGIN
    IF id IS NULL THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch'); END IF;
    RETURN yoao_assets_v1.record_operator_original_written(id, p_receipt);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.fail_import_original(p_scope jsonb, p_outcome text, p_code text, p_evidence jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE id uuid := yoao_assets_v1._import_builtin_original(p_scope, 'fail', NULL);
BEGIN
    IF id IS NULL THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch'); END IF;
    RETURN yoao_assets_v1.fail_operator_original(id, p_outcome, p_code, p_evidence);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1._import_io_job(uuid,uuid,text),
    yoao_assets_v1._import_io_transport(uuid,jsonb),
    yoao_assets_v1.import_saved_io_list(uuid,uuid,jsonb,jsonb),
    yoao_assets_v1.save_observed_dataset_import_manifest(uuid,uuid,jsonb),
    yoao_assets_v1._import_io_entry(uuid,bigint,uuid),
    yoao_assets_v1._import_io_authority(public.dataset_import_entries),
    yoao_assets_v1.begin_dataset_import_original(uuid,uuid,jsonb),
    yoao_assets_v1._import_io_target(public.managed_original_creations),
    yoao_assets_v1.open_dataset_import_original(uuid,uuid,bigint,uuid,uuid),
    yoao_assets_v1._import_io_attempt(jsonb,boolean),
    yoao_assets_v1.import_saved_io_action(jsonb,text,jsonb,jsonb),
    yoao_assets_v1._import_builtin_original(jsonb,text,jsonb),
    yoao_assets_v1.record_import_original_prepared(jsonb,jsonb),
    yoao_assets_v1.mark_import_original_writing(jsonb),
    yoao_assets_v1.record_import_original_written(jsonb,jsonb),
    yoao_assets_v1.fail_import_original(jsonb,text,text,jsonb)
FROM PUBLIC;
