-- Native Images and Media cache writers use a separate per-run control LOGIN.
-- Each key is frozen before physical PUT; an unknown PUT is never retired by
-- observing a Job status, a missing process, or an object listing.
CREATE TABLE aap_assets_v1.fixture_native_cache_targets (
    run_id uuid PRIMARY KEY,
    endpoint text NOT NULL,
    bucket text NOT NULL,
    storage_identity text NOT NULL CHECK (storage_identity ~ '^[0-9a-f]{64}$'),
    receipt_sha256 text NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
    birth_receipt text NOT NULL,
    connection_id uuid NOT NULL UNIQUE,
    location_id uuid NOT NULL UNIQUE,
    binding_generation bigint NOT NULL CHECK (binding_generation>0)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_cache_targets FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_cache_target()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_target_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_cache_target() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_cache_target_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_cache_targets FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_cache_target();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_target_location()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid;
BEGIN
    SELECT run_id INTO v_run FROM aap_assets_v1.fixture_native_cache_targets AS target
    WHERE (TG_TABLE_NAME='storage_connections' AND target.connection_id=OLD.id)
       OR (TG_TABLE_NAME='storage_locations' AND target.location_id=OLD.id);
    IF FOUND THEN
        IF TG_OP='DELETE' AND EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_native_target_physical_acknowledgements WHERE run_id=v_run
        ) AND COALESCE((
            SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
            FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
        ),false) THEN RETURN OLD; END IF;
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_location_immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_target_location() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_connection_immutable BEFORE UPDATE OR DELETE ON public.storage_connections
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_location();
-- aap-statement-break
CREATE TRIGGER fixture_native_location_immutable BEFORE UPDATE OR DELETE ON public.storage_locations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_location();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.register_fixture_native_cache_target(
    p_run uuid,p_endpoint text,p_bucket text,p_storage_identity text,p_receipt_sha256 text,
    p_birth text,p_connection uuid,p_location uuid,p_generation bigint
) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_role text; v_account jsonb; v_provider jsonb;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_cache_owner_required';
    END IF;
    IF p_run IS NULL OR p_endpoint IS NULL OR p_endpoint !~ '^https?://[^/]+$'
       OR p_bucket IS DISTINCT FROM 'fixture-media-' || pg_catalog.replace(p_run::text,'-','')
       OR p_storage_identity IS NULL OR p_storage_identity !~ '^[0-9a-f]{64}$'
       OR p_receipt_sha256 IS NULL OR p_receipt_sha256 !~ '^[0-9a-f]{64}$'
       OR p_birth IS NULL OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_birth,'UTF8')),'hex') <> p_receipt_sha256
       OR p_connection IS NULL OR p_location IS NULL OR p_generation IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_native_cache_target_invalid';
    END IF;
    IF pg_catalog.jsonb_typeof(p_birth::jsonb) IS DISTINCT FROM 'object'
       OR p_birth::jsonb ->> 'schema' IS DISTINCT FROM 'aap-fixture-native-output-target/v1'
       OR p_birth::jsonb ->> 'run_id' IS DISTINCT FROM p_run::text
       OR p_birth::jsonb ->> 'endpoint' IS DISTINCT FROM p_endpoint
       OR p_birth::jsonb ->> 'bucket' IS DISTINCT FROM p_bucket
       OR p_birth::jsonb ->> 'storage_identity' IS DISTINCT FROM p_storage_identity
       OR pg_catalog.jsonb_typeof(p_birth::jsonb -> 'provider') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_birth::jsonb -> 'accounts') IS DISTINCT FROM 'object'
       OR p_birth::jsonb -> 'initial_bucket_absent' IS DISTINCT FROM 'true'::jsonb
       OR p_birth::jsonb -> 'initial_inventory_empty' IS DISTINCT FROM 'true'::jsonb
       OR p_birth::jsonb ->> 'versioning' IS DISTINCT FROM 'Enabled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_birth_receipt_invalid';
    END IF;
    v_provider := p_birth::jsonb -> 'provider';
    IF NOT (v_provider ?& ARRAY['owner_nonce','container_id','network_id','volume_id'])
       OR (v_provider - ARRAY['owner_nonce','container_id','network_id','volume_id']) <> '{}'::jsonb
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_each(v_provider) AS item(key,value)
           WHERE pg_catalog.jsonb_typeof(item.value)<>'string' OR item.value #>> '{}'=''
       )
       OR ((p_birth::jsonb -> 'accounts') - ARRAY['writer','reader','cleanup']) <> '{}'::jsonb
       OR NOT (p_birth::jsonb -> 'accounts' ?& ARRAY['writer','reader','cleanup']) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_birth_receipt_invalid';
    END IF;
    FOREACH v_role IN ARRAY ARRAY['writer','reader','cleanup'] LOOP
        v_account := p_birth::jsonb -> 'accounts' -> v_role;
        IF pg_catalog.jsonb_typeof(v_account) IS DISTINCT FROM 'object'
           OR NOT (v_account ?& ARRAY['identity_sha256','policy_name','policy_sha256'])
           OR (v_account - ARRAY['identity_sha256','policy_name','policy_sha256']) <> '{}'::jsonb
           OR COALESCE(v_account ->> 'identity_sha256','') !~ '^[0-9a-f]{64}$'
           OR COALESCE(v_account ->> 'policy_sha256','') !~ '^[0-9a-f]{64}$'
           OR v_account ->> 'policy_name' IS DISTINCT FROM
              'fixture-media-' || (CASE v_role WHEN 'writer' THEN 'w' WHEN 'reader' THEN 'r' ELSE 'c' END)
              || '-' || pg_catalog.replace(p_run::text,'-','') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_birth_receipt_invalid';
        END IF;
    END LOOP;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    JOIN yoao_fixture_v1.fixture_original_targets AS original ON original.id=root.original_target_id
    JOIN public.storage_locations AS location ON location.id=original.location_id
    JOIN public.storage_connections AS original_connection ON original_connection.id=location.connection_id
    WHERE run.id=p_run AND run.state='open' AND root.state='open'
      AND ((run.family='owned' AND run.parent_run_id=root.id) OR
           (run.family='screenshot' AND run.id=root.id AND run.parent_run_id IS NULL))
      AND original_connection.config ->> 'endpoint'=p_endpoint AND original.bucket<>p_bucket
      AND root.original_storage_identity=original.storage_identity
      AND root.original_store_receipt_sha256=original.store_receipt_sha256
    FOR SHARE OF run,root,original,location;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-fixture-native-endpoint:' || p_endpoint || ':' || p_bucket,0));
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-fixture-native-location:' || p_location::text,0));
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-fixture-native-identity:' || p_storage_identity,0));
    IF NOT EXISTS (
        SELECT 1 FROM public.storage_connections AS connection
        JOIN public.storage_locations AS target_location ON target_location.connection_id=connection.id
        WHERE connection.id=p_connection AND target_location.id=p_location
          AND connection.kind='s3' AND connection.scope='global' AND connection.project_id IS NULL
          AND connection.revision=1 AND connection.secret_enc IS NOT NULL
          AND connection.config=pg_catalog.jsonb_build_object(
              'endpoint',p_endpoint,'bucket',p_bucket,'base_prefix','','use_ssl',false)
          AND target_location.kind='s3' AND target_location.config='{}'::jsonb
          AND target_location.enabled AND target_location.config_revision=1
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_location_changed'; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets AS target
        WHERE target.run_id=p_run AND target.endpoint=p_endpoint AND target.bucket=p_bucket
          AND target.storage_identity=p_storage_identity AND target.receipt_sha256=p_receipt_sha256
          AND target.birth_receipt=p_birth AND target.connection_id=p_connection
          AND target.location_id=p_location AND target.binding_generation=p_generation
    ) THEN RETURN; END IF;
    IF EXISTS (SELECT 1 FROM public.storage_connections
               WHERE id<>p_connection AND config ->> 'endpoint'=p_endpoint AND config ->> 'bucket'=p_bucket)
       OR EXISTS (SELECT 1 FROM public.storage_bindings WHERE location_id=p_location)
       OR EXISTS (SELECT 1 FROM public.dataset_asset_locations WHERE location_id=p_location)
       OR EXISTS (SELECT 1 FROM public.managed_original_creations
                  WHERE location_id=p_location OR storage_identity=p_storage_identity)
       OR EXISTS (SELECT 1 FROM public.task_originals
                  WHERE location_id=p_location OR storage_identity=p_storage_identity)
       OR EXISTS (SELECT 1 FROM public.thumbnail_outputs WHERE
            storage_ref ->> 'location_id'=p_location::text OR storage_ref ->> 'storage_identity'=p_storage_identity
            OR (storage_ref ->> 'endpoint_url'=p_endpoint AND storage_ref ->> 'bucket'=p_bucket))
       OR EXISTS (SELECT 1 FROM public.image_pyramid_outputs WHERE
            storage_ref ->> 'location_id'=p_location::text OR storage_ref ->> 'storage_identity'=p_storage_identity
            OR (storage_ref ->> 'endpoint_url'=p_endpoint AND storage_ref ->> 'bucket'=p_bucket))
       OR EXISTS (SELECT 1 FROM public.video_derivative_outputs WHERE
            storage_ref ->> 'location_id'=p_location::text OR storage_ref ->> 'storage_identity'=p_storage_identity
            OR (storage_ref ->> 'endpoint_url'=p_endpoint AND storage_ref ->> 'bucket'=p_bucket))
    THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_target_already_referenced'; END IF;
    INSERT INTO aap_assets_v1.fixture_native_cache_targets(
        run_id,endpoint,bucket,storage_identity,receipt_sha256,birth_receipt,connection_id,location_id,binding_generation
    ) VALUES (p_run,p_endpoint,p_bucket,p_storage_identity,p_receipt_sha256,p_birth,p_connection,p_location,p_generation)
    ON CONFLICT DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets AS target
        WHERE target.run_id=p_run AND target.endpoint=p_endpoint AND target.bucket=p_bucket
          AND target.storage_identity=p_storage_identity AND target.receipt_sha256=p_receipt_sha256
          AND target.birth_receipt=p_birth AND target.connection_id=p_connection AND target.location_id=p_location
          AND target.binding_generation=p_generation
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_target_changed';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.register_fixture_native_cache_target(uuid,text,text,text,text,text,uuid,uuid,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_cache_target_binding(p_run uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_cache_owner_required';
    END IF;
    RETURN (SELECT pg_catalog.jsonb_build_object(
        'run_id',target.run_id,'endpoint',target.endpoint,'bucket',target.bucket,
        'storage_identity',target.storage_identity,'receipt_sha256',target.receipt_sha256,
        'connection_id',target.connection_id,'location_id',target.location_id,
        'binding_generation',target.binding_generation
    ) FROM aap_assets_v1.fixture_native_cache_targets AS target WHERE target.run_id=p_run);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_cache_target_binding(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_native_cache_writes (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation > 0),
    lease_token uuid NOT NULL,
    creation_id uuid NOT NULL,
    source_operation_id uuid NOT NULL,
    object_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    state text NOT NULL CHECK (state IN ('pending','written','verified')),
    etag text,
    version_id text,
    UNIQUE (family,job_id,job_generation,lease_token,object_key),
    CHECK ((state='pending')=(etag IS NULL)),
    CHECK ((state='pending' AND version_id IS NULL) OR
           (state<>'pending' AND version_id IS NOT NULL AND version_id NOT IN ('','null')))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_cache_writes FROM PUBLIC;
-- aap-statement-break
CREATE INDEX fixture_native_cache_writes_source
ON aap_assets_v1.fixture_native_cache_writes(source_operation_id);
-- aap-statement-break
CREATE INDEX fixture_native_cache_writes_dataset
ON aap_assets_v1.fixture_native_cache_writes(run_id,dataset_id);
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_cache_write()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR
       (NEW.id,NEW.run_id,NEW.dataset_id,NEW.item_id,NEW.family,NEW.job_id,
        NEW.job_generation,NEW.lease_token,NEW.creation_id,NEW.source_operation_id,
        NEW.object_key,NEW.sha256,NEW.size_bytes)
       IS DISTINCT FROM
       (OLD.id,OLD.run_id,OLD.dataset_id,OLD.item_id,OLD.family,OLD.job_id,
        OLD.job_generation,OLD.lease_token,OLD.creation_id,OLD.source_operation_id,
        OLD.object_key,OLD.sha256,OLD.size_bytes)
       OR (OLD.etag IS NOT NULL AND (NEW.etag,NEW.version_id) IS DISTINCT FROM (OLD.etag,OLD.version_id))
       OR NOT ((OLD.state='pending' AND NEW.state='written')
            OR (OLD.state='written' AND NEW.state='verified')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_write_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_cache_write() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_cache_write_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_cache_writes FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_cache_write();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_native_cache_write(
    p_family text,p_job uuid,p_generation bigint,p_lease uuid,p_creation uuid,p_operation uuid,
    p_endpoint text,p_bucket text,p_receipt_sha256 text,p_key text,p_sha256 text,p_size bigint,p_id uuid
) RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
    v_prefix text;
    v_job record;
    v_suffix text;
BEGIN
    IF p_family NOT IN ('image','media') OR p_job IS NULL OR p_generation IS NULL
       OR p_generation<1 OR p_lease IS NULL OR p_creation IS NULL OR p_operation IS NULL
       OR p_id IS NULL OR p_key IS NULL OR p_size IS NULL OR p_size<=0
       OR p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_native_cache_write_invalid';
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family=p_family AND job_id=p_job;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_birth.run_id) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_cache_principal_required';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    JOIN aap_assets_v1.fixture_native_cache_targets AS target ON target.run_id=run.id
    WHERE run.id=v_birth.run_id AND run.state IN ('open','sealing')
      AND root.state IN ('open','sealing')
      AND target.endpoint=p_endpoint AND target.bucket=p_bucket
      AND target.receipt_sha256=p_receipt_sha256
      AND EXISTS (
          SELECT 1 FROM aap_assets_v1.fixture_native_media_executions AS execution
          JOIN aap_assets_v1.fixture_native_media_claims AS claim ON claim.execution_id=execution.id
          WHERE execution.run_id=v_birth.run_id AND execution.dataset_id=v_birth.dataset_id
            AND execution.family=p_family AND execution.job_id=p_job AND execution.state='running'
            AND claim.state='claimed' AND claim.job_generation=p_generation AND claim.lease_token=p_lease
            AND aap_assets_v1.fixture_native_execution_lock_held(execution.backend_pid,execution.id)
      ) FOR SHARE OF run,root,target;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_target_changed';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
    JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
    JOIN public.storage_operations AS operation ON operation.id=p_operation
      AND operation.location_id=creation.location_id
      AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
      AND operation.node_id='aap-assets-media'
    JOIN aap_assets_v1.fixture_native_media_sources AS source
      ON source.source_operation_id=operation.id AND source.creation_id=creation.id
    WHERE resource.run_id=v_birth.run_id AND resource.kind='creation'
      AND resource.creation_id=p_creation AND resource.item_id=v_birth.item_id
      AND resource.state='published' AND creation.status='verified'
      AND source.run_id=v_birth.run_id AND source.dataset_id=v_birth.dataset_id
      AND source.item_id=v_birth.item_id AND source.family=p_family
      AND source.job_id=p_job AND source.job_generation=p_generation
      AND source.lease_token=p_lease AND source.state='open'
    FOR SHARE OF operation,source;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_source_unproven';
    END IF;
    IF p_family='image' THEN
        SELECT kind,status,generation,lease_token,lease_expires_at,
            output_prefix,pyramid_asset_id,pyramid_generation
        INTO v_job FROM public.asset_image_jobs WHERE job_id=p_job FOR SHARE;
        IF NOT FOUND OR v_job.status<>'running' OR v_job.generation<>p_generation
           OR v_job.lease_token IS DISTINCT FROM p_lease
           OR v_job.lease_expires_at IS NULL
           OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
           OR v_job.kind NOT IN ('thumbnail','pyramid') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_lease_changed';
        END IF;
        v_prefix := CASE v_job.kind
            WHEN 'thumbnail' THEN 'image-assets/' || p_job::text || '/g' || p_generation::text || '/a' || p_lease::text || '/'
            ELSE 'image-pyramids/' || v_job.pyramid_asset_id::text || '/g' || v_job.pyramid_generation::text || '/a' || p_lease::text || '/'
        END;
        IF v_prefix IS NULL OR v_job.output_prefix IS DISTINCT FROM v_prefix THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_owner_changed';
        END IF;
    ELSE
        SELECT kind,status,generation,lease_token,lease_expires_at INTO v_job
        FROM public.asset_media_jobs WHERE job_id=p_job FOR SHARE;
        IF NOT FOUND OR v_job.status<>'running' OR v_job.generation<>p_generation
           OR v_job.lease_token IS DISTINCT FROM p_lease
           OR v_job.lease_expires_at IS NULL
           OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
           OR v_job.kind NOT IN ('video_metadata','video_timetable','frame','chunk') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_lease_changed';
        END IF;
        v_prefix := 'media-assets/' || p_job::text || '/g' || p_generation::text || '/a' || p_lease::text || '/';
    END IF;
    IF v_prefix IS NULL OR NOT pg_catalog.starts_with(p_key,v_prefix) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_owner_changed';
    END IF;
    v_suffix := pg_catalog.substr(p_key,pg_catalog.char_length(v_prefix)+1);
    IF NOT ((p_family='image' AND v_job.kind='thumbnail' AND v_suffix='thumbnail.webp')
       OR (p_family='image' AND v_job.kind='pyramid' AND (v_suffix IN ('manifest.json','overview.webp')
           OR v_suffix ~ '^tiles/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)[.]webp$'))
       OR (p_family='media' AND v_suffix IN ('poster.webp','playback.mp4','frame.webp','frame.jpeg','chunk.mp4'))) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_owner_changed';
    END IF;
    INSERT INTO aap_assets_v1.fixture_native_cache_writes
        (id,run_id,dataset_id,item_id,family,job_id,job_generation,lease_token,creation_id,
         source_operation_id,object_key,sha256,size_bytes,state)
    VALUES (p_id,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,p_family,p_job,p_generation,p_lease,
        p_creation,p_operation,p_key,p_sha256,p_size,'pending');
    RETURN p_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_native_cache_write(text,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.advance_fixture_native_cache_write(
    p_id uuid,p_source_operation uuid,p_state text,p_etag text,p_version text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_write aap_assets_v1.fixture_native_cache_writes%ROWTYPE;
BEGIN
    SELECT * INTO v_write FROM aap_assets_v1.fixture_native_cache_writes
    WHERE id=p_id AND source_operation_id=p_source_operation FOR UPDATE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_write.run_id) IS DISTINCT FROM true
       OR NOT EXISTS (
        SELECT 1 FROM public.storage_operations AS operation
        JOIN public.managed_original_creations AS creation ON creation.id=v_write.creation_id
        WHERE operation.id=p_source_operation AND operation.location_id=creation.location_id
          AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
          AND operation.node_id='aap-assets-media'
    ) THEN RETURN false; END IF;
    IF p_state='written' AND v_write.state='pending' AND p_etag IS NOT NULL AND p_etag<>''
       AND p_version IS NOT NULL AND p_version NOT IN ('','null') THEN
        UPDATE aap_assets_v1.fixture_native_cache_writes
        SET state='written',etag=p_etag,version_id=p_version WHERE id=p_id;
        RETURN FOUND;
    ELSIF p_state='verified' AND v_write.state='written'
       AND p_etag IS NOT DISTINCT FROM v_write.etag AND p_version IS NOT DISTINCT FROM v_write.version_id THEN
        UPDATE aap_assets_v1.fixture_native_cache_writes SET state='verified' WHERE id=p_id;
        RETURN FOUND;
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.advance_fixture_native_cache_write(uuid,uuid,text,text,text) FROM PUBLIC;
