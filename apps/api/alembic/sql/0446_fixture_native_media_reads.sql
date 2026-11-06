-- A fixture output reader has its own per-run function-only LOGIN.  The actual
-- response owns one durable permit until its physical S3 body is closed.
CREATE TABLE aap_assets_v1.fixture_native_read_principals (
    run_id uuid PRIMARY KEY,
    principal_oid oid NOT NULL UNIQUE,
    principal_name name NOT NULL UNIQUE,
    proxy_origin text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('active','retired'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_read_principals FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_read_principal()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE'
       OR (NEW.run_id,NEW.principal_oid,NEW.principal_name,NEW.proxy_origin) IS DISTINCT FROM
          (OLD.run_id,OLD.principal_oid,OLD.principal_name,OLD.proxy_origin)
       OR OLD.state<>'active' OR NEW.state<>'retired' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_reader_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_read_principal() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_read_principal_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_read_principals FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_read_principal();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.register_fixture_native_reader(p_run uuid,p_role name,p_proxy_origin text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_oid oid;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_reader_owner_required';
    END IF;
    IF p_run IS NULL OR p_role::text IS DISTINCT FROM
       'aap_fixture_media_read_' || pg_catalog.replace(p_run::text,'-','')
       OR p_proxy_origin IS NULL OR pg_catalog.char_length(p_proxy_origin)>2048
       OR p_proxy_origin !~ '^https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?$'
       THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=p_run AND run.state='open' AND root.state='open'
      AND ((run.family='owned' AND run.parent_run_id=root.id) OR
           (run.family='screenshot' AND run.id=root.id AND run.parent_run_id IS NULL))
    FOR SHARE OF run,root;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT role.oid INTO v_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname=p_role AND role.rolcanlogin AND NOT role.rolsuper
      AND NOT role.rolinherit AND NOT role.rolcreatedb AND NOT role.rolcreaterole
      AND NOT role.rolreplication AND NOT role.rolbypassrls;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_native_read_principals(run_id,principal_oid,principal_name,proxy_origin,state)
    VALUES (p_run,v_oid,p_role,p_proxy_origin,'active') ON CONFLICT DO NOTHING;
    RETURN EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_read_principals AS principal
        WHERE principal.run_id=p_run AND principal.principal_oid=v_oid
          AND principal.principal_name=p_role AND principal.proxy_origin=p_proxy_origin AND principal.state='active'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.register_fixture_native_reader(uuid,name,text) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_native_cache_reads (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    write_id uuid NOT NULL,
    evidence_kind text NOT NULL CHECK (evidence_kind IN ('native','legacy')),
    backend_pid integer NOT NULL CHECK (backend_pid>0),
    state text NOT NULL CHECK (state IN ('open','settled'))
);
-- aap-statement-break
CREATE INDEX fixture_native_cache_reads_dataset
ON aap_assets_v1.fixture_native_cache_reads(run_id,dataset_id,state);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_cache_reads FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_cache_read()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.id,NEW.run_id,NEW.dataset_id,NEW.write_id,NEW.evidence_kind,NEW.backend_pid)
       IS DISTINCT FROM (OLD.id,OLD.run_id,OLD.dataset_id,OLD.write_id,OLD.evidence_kind,OLD.backend_pid)
       OR OLD.state<>'open' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_read_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_cache_read() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_cache_read_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_cache_reads FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_cache_read();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_read_lock_held(p_pid integer,p_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-fixture-native-output-read:' || p_id::text,0) AS key
        ) AS lock_key
        WHERE held.locktype='advisory' AND held.pid=p_pid AND held.mode='ExclusiveLock'
          AND held.granted AND held.objsubid=1
          AND held.classid::bigint=((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint=(lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_read_lock_held(integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_native_output_read(p_run uuid,p_write uuid,p_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_write record;
        v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
BEGIN
    IF p_run IS NULL OR p_write IS NULL OR p_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_read_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=p_run AND principal.principal_name=role.rolname
          AND role.rolname=SESSION_USER AND principal.state='active'
    ) OR aap_assets_v1.fixture_native_read_lock_held(pg_catalog.pg_backend_pid(),p_id) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_reader_required';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=p_run AND run.state='open' AND root.state='open' FOR SHARE OF run,root;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed'; END IF;
    BEGIN
        SELECT * INTO STRICT v_write FROM (
            SELECT write.dataset_id,write.object_key,write.sha256,write.size_bytes,write.etag,write.version_id,
                   'native'::text AS evidence_kind
            FROM aap_assets_v1.fixture_native_cache_writes AS write
            WHERE write.id=p_write AND write.run_id=p_run AND write.state='verified'
            UNION ALL
            SELECT version.dataset_id,version.object_key,version.sha256,version.size_bytes,version.etag,version.version_id,
                   'legacy'::text AS evidence_kind
            FROM aap_assets_v1.fixture_media_versions AS version
            WHERE version.id=p_write AND version.run_id=p_run AND version.dataset_id IS NOT NULL
              AND version.state='verified' AND version.version_id IS NOT NULL AND version.version_id NOT IN ('','null')
        ) AS evidence;
    EXCEPTION WHEN no_data_found OR too_many_rows THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_missing';
    END;
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_missing'; END IF;
    INSERT INTO aap_assets_v1.fixture_native_cache_reads(id,run_id,dataset_id,write_id,evidence_kind,backend_pid,state)
    VALUES (p_id,p_run,v_write.dataset_id,p_write,v_write.evidence_kind,pg_catalog.pg_backend_pid(),'open');
    RETURN pg_catalog.jsonb_build_object(
        'id',p_id,'run_id',p_run,'dataset_id',v_write.dataset_id,'write_id',p_write,
        'endpoint',v_target.endpoint,'bucket',v_target.bucket,'storage_identity',v_target.storage_identity,
        'receipt_sha256',v_target.receipt_sha256,'key',v_write.object_key,
        'database_oid',(SELECT database.oid FROM pg_catalog.pg_database AS database
                        WHERE database.datname=pg_catalog.current_database()),
        'database_owner_marker',(SELECT pg_catalog.shobj_description(database.oid,'pg_database')
                                 FROM pg_catalog.pg_database AS database
                                 WHERE database.datname=pg_catalog.current_database()),
        'sha256',v_write.sha256,'size_bytes',v_write.size_bytes,'etag',v_write.etag,
        'version_id',v_write.version_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_native_output_read(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_native_output_read(p_run uuid,p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_read_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=p_run AND principal.principal_name=role.rolname
          AND role.rolname=SESSION_USER AND principal.state='active'
    ) OR aap_assets_v1.fixture_native_read_lock_held(pg_catalog.pg_backend_pid(),p_id) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    UPDATE aap_assets_v1.fixture_native_cache_reads SET state='settled'
    WHERE id=p_id AND run_id=p_run AND state='open' AND backend_pid=pg_catalog.pg_backend_pid();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_native_output_read(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retire_fixture_native_reader(p_run uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_state text;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_reader_owner_required';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=p_run AND ((run.parent_run_id IS NOT NULL AND run.family='owned')
        OR (run.parent_run_id IS NULL AND run.family='screenshot'))
      AND run.state='sealing' FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT state INTO v_state FROM aap_assets_v1.fixture_native_read_principals
    WHERE run_id=p_run FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF v_state='retired' THEN RETURN true; END IF;
    IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads
               WHERE run_id=p_run AND state<>'settled') THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_native_read_principals SET state='retired'
    WHERE run_id=p_run AND state='active';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retire_fixture_native_reader(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_reader_identity(p_run uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_principal aap_assets_v1.fixture_native_read_principals%ROWTYPE;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_reader_owner_required';
    END IF;
    SELECT * INTO v_principal FROM aap_assets_v1.fixture_native_read_principals WHERE run_id=p_run;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN pg_catalog.jsonb_build_object('oid',v_principal.principal_oid,
        'name',v_principal.principal_name,'proxy_origin',v_principal.proxy_origin,
        'state',v_principal.state);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_reader_identity(uuid) FROM PUBLIC;
