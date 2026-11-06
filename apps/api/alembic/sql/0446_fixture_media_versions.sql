-- Record exact producer intent, actual successful PUT response and verified
-- version before fixture cleanup can claim a physical output has retired.
CREATE TABLE aap_assets_v1.fixture_media_versions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid,
    item_id uuid,
    task_id uuid,
    execution_kind text NOT NULL CHECK (execution_kind IN ('backfill','pyramid','thumbnail','independent')),
    execution_id uuid,
    execution_token uuid,
    owner_kind text NOT NULL CHECK (owner_kind IN ('thumbnail','pyramid_output','video_output')),
    owner_id uuid NOT NULL,
    object_key text NOT NULL,
    key_digest bytea NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    state text NOT NULL CHECK (state IN ('pending','written','verified')),
    etag text,
    version_id text,
    CHECK ((dataset_id IS NOT NULL AND item_id IS NOT NULL) OR
           (dataset_id IS NULL AND item_id IS NULL AND task_id IS NOT NULL)),
    CHECK ((execution_kind='thumbnail' AND execution_id IS NULL AND execution_token IS NULL)
        OR (execution_kind IN ('backfill','pyramid','independent') AND execution_id IS NOT NULL AND execution_token IS NOT NULL)),
    CHECK ((state='pending') = (etag IS NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_media_versions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_media_version()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.id,NEW.run_id,NEW.dataset_id,NEW.item_id,NEW.task_id,
          NEW.execution_kind,NEW.execution_id,NEW.execution_token,NEW.owner_kind,NEW.owner_id,
          NEW.object_key,NEW.key_digest,NEW.sha256,NEW.size_bytes)
       IS DISTINCT FROM (OLD.id,OLD.run_id,OLD.dataset_id,OLD.item_id,OLD.task_id,
          OLD.execution_kind,OLD.execution_id,OLD.execution_token,OLD.owner_kind,OLD.owner_id,
          OLD.object_key,OLD.key_digest,OLD.sha256,OLD.size_bytes)
       OR (OLD.etag IS NOT NULL AND (NEW.etag,NEW.version_id) IS DISTINCT FROM (OLD.etag,OLD.version_id))
       OR NOT ((OLD.state='pending' AND NEW.state IN ('pending','written'))
            OR (OLD.state='written' AND NEW.state IN ('written','verified'))
            OR (OLD.state='verified' AND NEW.state='verified')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_version() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_version_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_media_versions FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_version();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_media_version(
    p_kind text,p_execution_id uuid,p_token uuid,p_owner_kind text,p_owner_id uuid,
    p_endpoint text,p_bucket text,p_object_key text,p_sha256 text,p_size bigint,p_id uuid
) RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
    v_prefix text; v_endpoint text; v_bucket text; v_suffix text; v_ref jsonb; v_saved record;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births
    WHERE owner_kind=p_owner_kind AND owner_id=p_owner_id AND generation=0 FOR UPDATE;
    IF NOT FOUND OR (CASE WHEN p_kind='independent' THEN
        aap_assets_v1.fixture_independent_session_authorized(
            p_execution_id,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,v_birth.task_id)
      ELSE aap_jobs_v1.fixture_media_session_authorized(
            p_kind,p_execution_id,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id)
      END) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    IF p_id IS NULL OR p_object_key IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$'
       OR p_size IS NULL OR p_size <= 0 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_version_invalid';
    END IF;
    IF p_owner_kind='thumbnail' THEN
        SELECT storage_ref ->> 'prefix',storage_ref ->> 'endpoint_url',storage_ref ->> 'bucket',storage_ref
        INTO v_prefix,v_endpoint,v_bucket,v_ref FROM public.thumbnail_outputs
        WHERE id=p_owner_id AND state='building';
    ELSIF p_owner_kind='pyramid_output' THEN
        SELECT storage_ref ->> 'prefix',storage_ref ->> 'endpoint_url',storage_ref ->> 'bucket',storage_ref
        INTO v_prefix,v_endpoint,v_bucket,v_ref FROM public.image_pyramid_outputs
        WHERE id=p_owner_id AND state='building';
    ELSIF p_owner_kind='video_output' THEN
        SELECT storage_ref ->> 'prefix',storage_ref ->> 'endpoint_url',storage_ref ->> 'bucket',storage_ref
        INTO v_prefix,v_endpoint,v_bucket,v_ref FROM public.video_derivative_outputs
        WHERE id=p_owner_id AND state='building';
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_version_invalid';
    END IF;
    IF NOT FOUND OR v_endpoint IS DISTINCT FROM p_endpoint OR v_bucket IS DISTINCT FROM p_bucket
       OR NOT pg_catalog.starts_with(p_object_key,v_prefix) OR NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets AS target
           WHERE target.run_id=v_birth.run_id AND target.endpoint=p_endpoint AND target.bucket=p_bucket
             AND target.location_id::text=v_ref ->> 'location_id'
             AND target.binding_generation::text=v_ref ->> 'binding_generation'
             AND target.storage_identity=v_ref ->> 'storage_identity'
       ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_owner_changed';
    END IF;
    v_suffix := pg_catalog.substr(p_object_key,pg_catalog.char_length(v_prefix)+1);
    IF NOT ((p_owner_kind='thumbnail' AND v_suffix='thumbnail.webp')
       OR (p_owner_kind='pyramid_output' AND (v_suffix IN ('manifest.json','overview.webp')
           OR v_suffix ~ '^tiles/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)[.]webp$'))
       OR (p_owner_kind='video_output' AND v_suffix IN
           ('metadata.json','poster.webp','frame.webp','frame.jpeg','playback.mp4','chunk.mp4'))) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_owner_changed';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_versions AS previous
        WHERE previous.owner_kind=p_owner_kind AND previous.owner_id=p_owner_id
          AND previous.key_digest=pg_catalog.sha256(pg_catalog.convert_to(p_object_key,'UTF8'))
          AND previous.object_key=p_object_key AND previous.id <> p_id
          AND (previous.state <> 'verified' OR previous.version_id IS NULL
               OR previous.version_id='null')
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_previous_write_unsettled';
    END IF;
    INSERT INTO aap_assets_v1.fixture_media_versions
        (id,run_id,dataset_id,item_id,task_id,execution_kind,execution_id,execution_token,
         owner_kind,owner_id,object_key,key_digest,sha256,size_bytes,state)
    VALUES (p_id,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,v_birth.task_id,p_kind,p_execution_id,p_token,
        p_owner_kind,p_owner_id,p_object_key,
        pg_catalog.sha256(pg_catalog.convert_to(p_object_key,'UTF8')),p_sha256,p_size,'pending')
    ON CONFLICT (id) DO NOTHING;
    SELECT id,object_key,sha256,size_bytes,state INTO v_saved
    FROM aap_assets_v1.fixture_media_versions
    WHERE id=p_id AND owner_kind=p_owner_kind AND owner_id=p_owner_id
      AND key_digest=pg_catalog.sha256(pg_catalog.convert_to(p_object_key,'UTF8')) FOR UPDATE;
    IF NOT FOUND OR v_saved.id IS DISTINCT FROM p_id OR v_saved.object_key IS DISTINCT FROM p_object_key
       OR v_saved.sha256 IS DISTINCT FROM p_sha256 OR v_saved.size_bytes IS DISTINCT FROM p_size
       OR v_saved.state <> 'pending' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_conflict';
    END IF;
    RETURN v_saved.id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_media_version(text,uuid,uuid,text,uuid,text,text,text,text,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.record_fixture_media_version(
    p_kind text,p_execution_id uuid,p_token uuid,p_id uuid,p_etag text,p_version text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scope record;
BEGIN
    SELECT run_id,dataset_id,item_id,task_id INTO v_scope
    FROM aap_assets_v1.fixture_media_versions
    WHERE id=p_id AND execution_kind=p_kind AND execution_id=p_execution_id
      AND execution_token=p_token FOR UPDATE;
    IF NOT FOUND OR (CASE WHEN p_kind='independent' THEN
        aap_assets_v1.fixture_independent_session_authorized(
            p_execution_id,p_token,v_scope.run_id,v_scope.dataset_id,v_scope.item_id,v_scope.task_id)
      ELSE aap_jobs_v1.fixture_media_session_authorized(
            p_kind,p_execution_id,p_token,v_scope.run_id,v_scope.dataset_id,v_scope.item_id)
      END) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF p_etag IS NULL OR p_etag='' OR p_version IS NULL OR p_version IN ('','null') THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_media_versions SET state='written',etag=p_etag,version_id=p_version
    WHERE id=p_id AND state='pending';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_fixture_media_version(text,uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.verify_fixture_media_version(
    p_kind text,p_execution_id uuid,p_token uuid,p_id uuid,p_version text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scope record;
BEGIN
    SELECT run_id,dataset_id,item_id,task_id INTO v_scope
    FROM aap_assets_v1.fixture_media_versions
    WHERE id=p_id AND execution_kind=p_kind AND execution_id=p_execution_id
      AND execution_token=p_token FOR UPDATE;
    IF NOT FOUND OR (CASE WHEN p_kind='independent' THEN
        aap_assets_v1.fixture_independent_session_authorized(
            p_execution_id,p_token,v_scope.run_id,v_scope.dataset_id,v_scope.item_id,v_scope.task_id)
      ELSE aap_jobs_v1.fixture_media_session_authorized(
            p_kind,p_execution_id,p_token,v_scope.run_id,v_scope.dataset_id,v_scope.item_id)
      END) IS DISTINCT FROM true THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_media_versions SET state='verified'
    WHERE id=p_id AND state='written' AND version_id IS NOT DISTINCT FROM p_version;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.verify_fixture_media_version(text,uuid,uuid,uuid,text) FROM PUBLIC;
