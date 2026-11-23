CREATE TABLE public.training_saved_objects (
    location_id uuid NOT NULL,
    bucket text NOT NULL,
    object_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    version_id text,
    etag text,
    signed_until timestamptz,
    PRIMARY KEY (location_id, bucket, object_key)
);

-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_saved_owner_scope(
    p_scope text, p_owner uuid, p_attempt uuid, p_member text,
    p_action text, p_purpose text, p_evidence jsonb
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $scope$
DECLARE
    ref jsonb;
    entry jsonb;
    digest text;
    object_size bigint;
    object_key text;
    entity uuid;
    metadata jsonb := '{}'::jsonb;
    retired boolean := false;
    prior_attempt boolean := false;
    build boolean;
    execution jsonb;
    v public.training_dataset_versions%ROWTYPE;
    r public.training_runs%ROWTYPE;
    a public.training_attempts%ROWTYPE;
    p public.training_previews%ROWTYPE;
    e public.sample_evaluation_runs%ROWTYPE;
    ea public.sample_evaluation_attempts%ROWTYPE;
    la public.active_learning_attempts%ROWTYPE;
    l public.active_learning_rounds%ROWTYPE;
    d public.training_storage_deletions%ROWTYPE;
BEGIN
    CASE p_scope
    WHEN 'dataset' THEN
        SELECT * INTO v FROM public.training_dataset_versions WHERE id=p_owner;
        ref := v.storage_ref;
        build := p_purpose = 'training_dataset_' || replace(p_owner::text, '-', '');
        IF p_action='retire' THEN
            IF NOT build OR p_attempt IS NULL OR p_member IS NOT NULL
               OR (p_attempt=v.attempt_id AND v.status NOT IN ('failed','cancelled'))
               OR v.package_key = (ref->>'prefix') || 'attempts/' || p_attempt::text || '/dataset.zip' THEN
                RETURN NULL;
            END IF;
            prior_attempt := p_attempt IS DISTINCT FROM v.attempt_id;
            object_key := (ref->>'prefix') || 'attempts/' || p_attempt::text || '/';
            retired := true;
        ELSIF build AND p_action IN ('upload','head','download') THEN
            IF p_attempt IS DISTINCT FROM v.attempt_id OR v.status <> 'running'
               OR v.async_job_id IS NULL THEN RETURN NULL; END IF;
            execution := jsonb_build_array(v.attempt_id,v.async_job_id,v.snapshot_digest);
            IF p_member='archive' THEN
                object_key := (ref->>'prefix') || 'attempts/' || p_attempt::text || '/dataset.zip';
                IF p_action='upload' AND (p_evidence->>'size_bytes')::bigint > 24::bigint*1024^3 THEN RETURN NULL; END IF;
            ELSE
                IF p_member !~ '^(media|mask):[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN RETURN NULL; END IF;
                entity := split_part(p_member, ':', 2)::uuid;
                SELECT value INTO entry FROM jsonb_array_elements(v.snapshot->'samples')
                  WHERE value->>'task_id'=entity::text;
                IF entry IS NULL THEN RETURN NULL; END IF;
                IF split_part(p_member, ':', 1)='media' THEN
                    object_key := (ref->>'prefix') || 'media/' || entity::text;
                    digest := coalesce(entry#>>'{original,sha256}', entry#>>'{asset_reference,sha256}');
                    object_size := (entry#>>'{media,size_bytes}')::bigint;
                    metadata := jsonb_build_object('snapshot-digest',v.snapshot_digest);
                ELSE
                    IF entry->'annotations' IS NULL THEN RETURN NULL; END IF;
                    object_key := (ref->>'prefix') || 'masks/' || entity::text || '.json';
                    IF p_action='upload' AND (p_evidence->>'size_bytes')::bigint > 64*1024^2 THEN RETURN NULL; END IF;
                END IF;
            END IF;
        ELSIF v.status='ready' AND p_action IN ('head','download','sign_download')
            AND p_purpose IN ('training_dataset_download','training_dataset_media','training_dataset_mask',
                'training_assignment','training_preview_assignment','sample_evaluation_source','training_model_url') THEN
            IF p_member='archive' THEN
                IF p_attempt IS DISTINCT FROM v.attempt_id THEN RETURN NULL; END IF;
                object_key := (ref->>'prefix') || 'attempts/' || v.attempt_id::text || '/dataset.zip';
                IF object_key IS DISTINCT FROM v.package_key THEN RETURN NULL; END IF;
                digest := v.package_sha256;
                object_size := v.size_bytes;
            ELSE
                IF p_member !~ '^(media|mask):[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN RETURN NULL; END IF;
                entity := split_part(p_member, ':', 2)::uuid;
                SELECT value INTO entry FROM jsonb_array_elements(v.manifest->'samples')
                  WHERE value->>'task_id'=entity::text;
                IF entry IS NULL THEN RETURN NULL; END IF;
                IF split_part(p_member, ':', 1)='media' THEN
                    object_key := (ref->>'prefix') || 'media/' || entity::text;
                    digest := entry->>'sha256';
                    object_size := (entry->>'size_bytes')::bigint;
                    metadata := jsonb_build_object('snapshot-digest',v.snapshot_digest);
                ELSE
                    IF p_action='sign_download' THEN RETURN NULL; END IF;
                    object_key := (ref->>'prefix') || 'masks/' || entity::text || '.json';
                    digest := entry->>'label_sha256';
                END IF;
            END IF;
            IF digest IS NULL THEN RETURN NULL; END IF;
        ELSE RETURN NULL;
        END IF;
    WHEN 'run' THEN
        SELECT * INTO r FROM public.training_runs WHERE id=p_owner;
        SELECT * INTO a FROM public.training_attempts WHERE id=p_attempt AND run_id=p_owner;
        ref := r.storage_ref;
        IF a.id IS NULL OR p_member !~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$'
           OR position('..' IN p_member)>0 THEN RETURN NULL; END IF;
        object_key := (ref->>'prefix') || 'attempts/' || a.id::text || '/' || p_member;
        metadata := jsonb_build_object('attempt-id',a.id);
        IF p_action='sign_upload' OR p_purpose='training_artifact_verify' THEN
            IF NOT ((p_action='sign_upload' AND p_purpose='training_upload_urls')
                    OR (p_purpose='training_artifact_verify' AND p_action IN ('head','download'))) THEN RETURN NULL; END IF;
            IF r.current_attempt_id IS DISTINCT FROM a.id OR a.lease_digest IS NULL
               OR a.boot_id::text IS DISTINCT FROM p_evidence->>'boot'
               OR a.status NOT IN ('preparing','training','evaluating','uploading','lost','cancelling')
               OR NOT EXISTS (SELECT FROM public.training_resource_leases lease
                   WHERE lease.resource_id=a.resource_id AND lease.attempt_id=a.id
                     AND lease.boot_id=a.boot_id AND lease.executor_id=a.executor_id) THEN RETURN NULL; END IF;
            execution := jsonb_build_array(a.id,a.boot_id,a.claim_id);
            entry := a.upload_inventory->p_member;
        ELSIF p_action IN ('head','download','sign_download')
          AND p_purpose IN ('training_artifact_download','training_assignment','training_model_url',
                            'training_model_read','training_evaluation_read','training_initialization_read',
                            'training_checkpoint_read') THEN
            entry := a.artifacts->p_member;
            IF (entry->>'key') IS DISTINCT FROM object_key THEN RETURN NULL; END IF;
        ELSE RETURN NULL;
        END IF;
        digest := entry->>'sha256';
        object_size := (entry->>'size_bytes')::bigint;
    WHEN 'preview' THEN
        SELECT * INTO p FROM public.training_previews WHERE id=p_owner;
        ref := p.storage_ref;
        IF p_action='retire' THEN
            IF p_purpose<>'training_preview_cleanup' OR p.status <> 'expired'
               OR p.expires_at > now() OR p.put_expires_at >= now()
               OR EXISTS (SELECT FROM public.training_preview_resource_leases lease WHERE lease.preview_id=p.id)
               OR p_member IS NOT NULL THEN RETURN NULL; END IF;
            object_key := ref->>'prefix';
            retired := true;
        ELSE
            IF p_member !~ '^(preview-[0-3]\.png|report\.json)$' OR p.expires_at <= now() THEN RETURN NULL; END IF;
            object_key := (ref->>'prefix') || p_member;
            metadata := jsonb_build_object('preview-id',p.id,'input-digest',p.input_digest);
            IF p_action='sign_upload' OR p_purpose='training_preview_verify' THEN
                IF NOT ((p_action='sign_upload' AND p_purpose='training_preview_upload_urls' AND p.put_expires_at > now())
                    OR (p_action IN ('head','download') AND p_purpose='training_preview_verify')) THEN RETURN NULL; END IF;
                IF p.status <> 'running' OR p.lease_digest IS NULL
                  OR p.boot_id::text IS DISTINCT FROM p_evidence->>'boot'
                  OR NOT EXISTS (SELECT FROM public.training_preview_resource_leases lease
                    WHERE lease.resource_id=p.resource_id AND lease.preview_id=p.id
                      AND lease.boot_id=p.boot_id AND lease.executor_id=p.executor_id) THEN RETURN NULL; END IF;
                entry := p.upload_inventory->p_member;
                execution := jsonb_build_array(p.id,p.boot_id,p.claim_id);
            ELSIF p_action IN ('head','download') AND p.status='succeeded' THEN
                entry := p.artifacts->p_member;
                IF (entry->>'key') IS DISTINCT FROM object_key THEN RETURN NULL; END IF;
            ELSE RETURN NULL;
            END IF;
            digest := entry->>'sha256';
            object_size := (entry->>'size_bytes')::bigint;
        END IF;
    WHEN 'evaluation' THEN
        SELECT * INTO e FROM public.sample_evaluation_runs WHERE id=p_owner;
        ref := e.storage_ref;
        IF p_action='retire' THEN
            IF p_purpose<>'sample_evaluation_cleanup' OR p_member IS NOT NULL
               OR e.deletion_requested_at IS NULL OR e.status NOT IN ('succeeded','failed','cancelled') THEN RETURN NULL; END IF;
            object_key := ref->>'prefix';
            retired := true;
        ELSE
            SELECT * INTO ea FROM public.sample_evaluation_attempts WHERE id=p_attempt AND run_id=p_owner;
            IF ea.id IS NULL OR p_member !~ '^[0-9a-f]{64}$' THEN RETURN NULL; END IF;
            object_key := (ref->>'prefix') || ea.id::text || '/' || p_member || '.json';
            digest := p_member;
            metadata := jsonb_build_object('attempt-id',ea.id,'evaluation-id',e.id,
                'spec-digest',e.spec_digest,'benchmark-digest',e.spec->>'benchmark_digest',
                'weight-sha256',e.spec#>>'{model_ref,sha256}');
            IF p_action='discard_report' THEN
                IF p_purpose<>'sample_evaluation_cleanup' OR (ea.report->>'key')=object_key
                   OR (e.deletion_requested_at IS NULL AND e.current_attempt_id=ea.id
                       AND e.status IN ('running','cancelling')) THEN RETURN NULL; END IF;
            ELSIF e.deletion_requested_at IS NOT NULL THEN RETURN NULL;
            ELSIF p_purpose='sample_evaluation_write' AND p_action IN ('upload','download','head') THEN
                IF e.current_attempt_id IS DISTINCT FROM ea.id OR ea.generation<>e.generation
                   OR e.status NOT IN ('running','cancelling') OR ea.claim_id IS NULL THEN RETURN NULL; END IF;
                IF p_action='upload' AND (p_evidence->>'size_bytes')::bigint > 8*1024^2 THEN RETURN NULL; END IF;
                execution := jsonb_build_array(ea.id,ea.generation,ea.claim_id);
            ELSIF p_purpose='sample_evaluation_read' AND p_action IN ('head','download') THEN
                IF ea.status NOT IN ('succeeded','failed','cancelled')
                   OR (ea.report->>'key') IS DISTINCT FROM object_key
                   OR (ea.report->>'sha256') IS DISTINCT FROM digest THEN RETURN NULL; END IF;
                object_size := (ea.report->>'size_bytes')::bigint;
            ELSE RETURN NULL;
            END IF;
        END IF;
    WHEN 'learning' THEN
        SELECT * INTO l FROM public.active_learning_rounds WHERE id=p_owner;
        ref := l.storage_ref;
        IF p_purpose<>'active_learning_media' OR p_member !~ '^(0|[1-9][0-9]{0,4})$'
           OR p_action NOT IN ('upload','head','download') THEN RETURN NULL; END IF;
        entry := l.spec->'samples'->(p_member::integer);
        IF entry->>'ordinal' IS DISTINCT FROM p_member THEN RETURN NULL; END IF;
        SELECT * INTO la FROM public.active_learning_attempts attempt
            WHERE attempt.id=p_attempt AND attempt.round_id=l.id;
        IF l.current_attempt_id IS DISTINCT FROM p_attempt OR l.status <> 'running'
           OR la.id IS NULL OR la.generation<>l.generation
           OR la.claim_id IS NULL OR la.status<>'running' THEN RETURN NULL; END IF;
        execution := jsonb_build_array(la.id,la.generation,la.claim_id);
        object_key := (ref->>'prefix') || 'media/' || p_member || '.png';
        digest := entry->>'derived_sha256';
        metadata := jsonb_build_object('round-id',l.id,'ordinal',p_member,
            'source-sha256',entry->>'sha256','sha256',digest);
        IF p_action='upload' AND (p_evidence->>'size_bytes')::bigint > 64*1024^2 THEN RETURN NULL; END IF;
    WHEN 'deletion' THEN
        SELECT * INTO d FROM public.training_storage_deletions WHERE id=p_owner;
        ref := d.storage_ref;
        IF p_action<>'retire' OR p_purpose<>'training_cleanup' OR d.last_attempted_at IS NULL
           OR p_member IS NOT NULL OR d.prefix IS DISTINCT FROM ref->>'prefix'
           OR d.bucket IS DISTINCT FROM ref->>'bucket' THEN RETURN NULL; END IF;
        entity := right(rtrim(d.prefix, '/'), 36)::uuid;
        IF EXISTS (SELECT FROM public.training_dataset_versions WHERE id=entity AND storage_ref=ref)
           OR EXISTS (SELECT FROM public.training_runs WHERE id=entity AND storage_ref=ref) THEN RETURN NULL; END IF;
        object_key := ref->>'prefix';
        retired := true;
    ELSE RETURN NULL;
    END CASE;
    IF ref->>'schema' IS DISTINCT FROM '2' OR object_key IS NULL
       OR NOT starts_with(object_key, ref->>'key_prefix') THEN RETURN NULL; END IF;
    IF p_action='upload' THEN
        IF (p_evidence->>'sha256') !~ '^[0-9a-f]{64}$' OR (p_evidence->>'size_bytes') !~ '^[0-9]{1,12}$'
           OR p_evidence->>'sha256' IS NULL OR p_evidence->>'size_bytes' IS NULL
           OR (digest IS NOT NULL AND digest<>p_evidence->>'sha256')
           OR (object_size IS NOT NULL AND object_size<>(p_evidence->>'size_bytes')::bigint) THEN RETURN NULL; END IF;
        digest := p_evidence->>'sha256';
        object_size := (p_evidence->>'size_bytes')::bigint;
    END IF;
    IF NOT retired AND (p_scope IN ('run','learning') OR p_scope='preview') AND digest IS NULL THEN RETURN NULL; END IF;
    RETURN jsonb_build_object('ref',ref,'key',object_key,'sha256',digest,'size_bytes',object_size,
        'meta',metadata,'prior_attempt',prior_attempt,'execution',execution);
END
$scope$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.training_saved_owner_scope(text,uuid,uuid,text,text,text,jsonb) FROM PUBLIC;

-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.capture_training_saved_io(
    p_permit uuid, p_scope text, p_owner uuid, p_attempt uuid, p_member text, p_action text, p_evidence jsonb
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $capture$
DECLARE
    permit public.storage_operations%ROWTYPE;
    location public.storage_locations%ROWTYPE;
    connection_rev bigint;
    connection_config jsonb;
    target jsonb;
    ref jsonb;
    root text;
    endpoint text;
    known public.training_saved_objects%ROWTYPE;
BEGIN
    IF p_action IS NULL OR p_action NOT IN ('head','download','upload','sign_upload','sign_download','retire','discard_report') THEN RETURN NULL; END IF;
    SELECT op.* INTO permit FROM public.storage_operations op
      JOIN public.storage_maintenance_gates gate ON gate.location_id=op.location_id AND gate.revision=op.revision
      WHERE op.id=p_permit AND gate.state='active';
    IF permit.id IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO location FROM public.storage_locations l
        WHERE l.id=permit.location_id AND l.enabled AND l.kind='s3' AND l.connection_id IS NOT NULL;
    IF location.id IS NULL THEN RETURN NULL; END IF;
    SELECT c.revision,c.config INTO connection_rev,connection_config FROM public.storage_connections c
        WHERE c.id=location.connection_id AND c.kind='s3' AND c.scope='global' AND c.project_id IS NULL;
    IF connection_rev IS NULL THEN RETURN NULL; END IF;
    root := concat(nullif(rtrim(connection_config->>'base_prefix','/'), '') || '/', '_aap_managed/training/');
    endpoint := rtrim(connection_config->>'endpoint', '/');
    IF position('://' IN endpoint)=0 THEN
        endpoint := CASE WHEN connection_config->>'use_ssl'='true' THEN 'https://' ELSE 'http://' END || endpoint;
    END IF;
    target := yoao_learning_v1.training_saved_owner_scope(
        p_scope,p_owner,p_attempt,p_member,p_action,permit.purpose,p_evidence);
    ref := target->'ref';
    IF target IS NULL OR (ref->>'location_id') IS DISTINCT FROM location.id::text
       OR (ref->>'key_prefix') IS DISTINCT FROM root
       OR (ref->>'bucket') IS DISTINCT FROM connection_config->>'bucket'
       OR (ref->>'endpoint_url') IS DISTINCT FROM endpoint THEN RETURN NULL; END IF;
    SELECT * INTO known FROM public.training_saved_objects s WHERE s.location_id=location.id
      AND s.bucket=connection_config->>'bucket' AND s.object_key=target->>'key';
    IF known.object_key IS NOT NULL AND ((target->>'sha256' IS NOT NULL AND known.sha256<>target->>'sha256')
        OR (target->>'size_bytes' IS NOT NULL AND known.size_bytes<>(target->>'size_bytes')::bigint)) THEN RETURN NULL; END IF;
    IF p_action='discard_report' AND known.etag IS NULL THEN RETURN NULL; END IF;
    IF p_action='retire' AND (target->>'prior_attempt')::boolean AND NOT EXISTS (
        SELECT FROM public.training_saved_objects s WHERE s.location_id=location.id
            AND s.bucket=connection_config->>'bucket' AND s.object_key=(target->>'key') || 'dataset.zip'
    ) THEN RETURN NULL; END IF;
    IF p_action='retire' AND EXISTS (
        SELECT FROM public.training_saved_objects s WHERE s.location_id=location.id
            AND s.bucket=connection_config->>'bucket' AND starts_with(s.object_key,target->>'key')
            AND s.signed_until>=now()
    ) THEN RETURN NULL; END IF;
    RETURN target - 'ref' || jsonb_build_object(
        'location_id',location.id,'config_revision',location.config_revision,'connection_revision',connection_rev,
        'bucket',connection_config->>'bucket','root',root,'gate_revision',permit.revision,
        'version',known.version_id,'etag',known.etag,
        'sha256',coalesce(target->>'sha256',known.sha256),
        'size_bytes',coalesce((target->>'size_bytes')::bigint,known.size_bytes));
END
$capture$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb) FROM PUBLIC;

-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_training_saved_io(
    p_permit uuid, p_scope text, p_owner uuid, p_attempt uuid, p_member text, p_action text,
    p_evidence jsonb, p_receipt jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $record$
DECLARE
    fact jsonb;
    exact_size bigint;
    grant_until timestamptz;
BEGIN
    IF p_action NOT IN ('upload','sign_upload','sign_download','download','retire','discard_report') THEN RETURN NULL; END IF;
    fact := yoao_assets_v1.capture_training_saved_io(p_permit,p_scope,p_owner,p_attempt,p_member,p_action,p_evidence);
    IF fact IS NULL THEN RETURN NULL; END IF;
    IF p_action IN ('retire','discard_report') THEN
        IF p_receipt IS DISTINCT FROM '{"retired":true}'::jsonb THEN RETURN NULL; END IF;
        DELETE FROM public.training_saved_objects s
        WHERE s.location_id=(fact->>'location_id')::uuid AND s.bucket=fact->>'bucket'
          AND CASE WHEN p_action='retire' THEN starts_with(s.object_key,fact->>'key')
                   ELSE s.object_key=fact->>'key' END;
        RETURN fact;
    END IF;
    IF fact->>'sha256' IS NULL THEN RETURN NULL; END IF;
    exact_size := coalesce((fact->>'size_bytes')::bigint, (p_receipt->>'size_bytes')::bigint);
    IF exact_size IS NULL THEN RETURN NULL; END IF;
    IF p_action='sign_upload' THEN
        IF p_receipt IS NOT NULL OR (p_evidence->>'expires_in')::integer NOT BETWEEN 1 AND
           (CASE WHEN p_scope='preview' THEN 60 ELSE 600 END) THEN RETURN NULL; END IF;
        grant_until := clock_timestamp() + make_interval(secs => (p_evidence->>'expires_in')::integer + 2);
    END IF;
    IF p_receipt IS NOT NULL AND ((p_receipt->>'sha256') IS DISTINCT FROM fact->>'sha256'
         OR (p_receipt->>'size_bytes')::bigint IS DISTINCT FROM exact_size
         OR length(coalesce(p_receipt->>'etag','')) NOT BETWEEN 1 AND 256
         OR length(coalesce(p_receipt->>'version','')) > 1024) THEN RETURN NULL; END IF;
    INSERT INTO public.training_saved_objects AS s
        (location_id,bucket,object_key,sha256,size_bytes,version_id,etag,signed_until)
    VALUES ((fact->>'location_id')::uuid,fact->>'bucket',fact->>'key',fact->>'sha256',exact_size,
      p_receipt->>'version',p_receipt->>'etag',grant_until)
    ON CONFLICT (location_id,bucket,object_key) DO UPDATE SET
      version_id=CASE WHEN s.etag IS NULL THEN EXCLUDED.version_id ELSE s.version_id END,
      etag=coalesce(s.etag, EXCLUDED.etag), signed_until=greatest(s.signed_until, EXCLUDED.signed_until)
      WHERE s.sha256=EXCLUDED.sha256 AND s.size_bytes=EXCLUDED.size_bytes
        AND ((s.etag IS NULL AND EXCLUDED.etag IS NOT NULL) OR EXCLUDED.signed_until IS NOT NULL);
    RETURN yoao_assets_v1.capture_training_saved_io(p_permit,p_scope,p_owner,p_attempt,p_member,p_action,p_evidence);
END
$record$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.record_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb,jsonb) FROM PUBLIC;
