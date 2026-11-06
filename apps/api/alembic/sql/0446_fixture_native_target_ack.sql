-- The provider owner supplies its immutable physical deletion receipt only
-- after the database owner has frozen the exact native target proof.
CREATE FUNCTION aap_assets_v1.acknowledge_fixture_native_target_physical_retirement(
    p_receipt text,p_receipt_sha256 text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_receipt jsonb; v_native jsonb; v_run uuid;
    v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
    v_ack aap_assets_v1.fixture_native_target_physical_acknowledgements%ROWTYPE;
BEGIN
    IF NOT COALESCE((SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')),false)
    THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_private_owner_required'; END IF;
    IF p_receipt IS NULL OR p_receipt_sha256 !~ '^[0-9a-f]{64}$'
       OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_receipt,'UTF8')),'hex') IS DISTINCT FROM p_receipt_sha256
    THEN RETURN false; END IF;
    v_receipt := p_receipt::jsonb;
    IF pg_catalog.jsonb_typeof(v_receipt) IS DISTINCT FROM 'object'
       OR NOT (v_receipt ?& ARRAY['schema','native_receipt','provider','bucket','accounts','bucket_absent','iam_absent'])
       OR (v_receipt - ARRAY['schema','native_receipt','provider','bucket','accounts','bucket_absent','iam_absent']) <> '{}'::jsonb
       OR v_receipt ->> 'schema' IS DISTINCT FROM 'aap-fixture-native-target-physical-retirement/v1'
       OR v_receipt -> 'bucket_absent' IS DISTINCT FROM 'true'::jsonb
       OR v_receipt -> 'iam_absent' IS DISTINCT FROM pg_catalog.jsonb_build_object(
           'writer:user',true,'writer:policy',true,'reader:user',true,'reader:policy',true,
           'cleanup:user',true,'cleanup:policy',true)
       OR pg_catalog.jsonb_typeof(v_receipt -> 'native_receipt') IS DISTINCT FROM 'object'
    THEN RETURN false; END IF;
    v_native := v_receipt -> 'native_receipt';
    IF COALESCE(v_native ->> 'run_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    THEN RETURN false; END IF;
    v_run := (v_native ->> 'run_id')::uuid;
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=v_run FOR UPDATE;
    IF NOT FOUND OR aap_assets_v1.fixture_native_target_receipt(v_run) IS DISTINCT FROM v_native
       OR v_receipt -> 'provider' IS DISTINCT FROM (v_target.birth_receipt::jsonb -> 'provider')
       OR v_receipt -> 'accounts' IS DISTINCT FROM (v_target.birth_receipt::jsonb -> 'accounts')
       OR v_receipt ->> 'bucket' IS DISTINCT FROM v_target.bucket THEN RETURN false; END IF;
    SELECT * INTO v_ack FROM aap_assets_v1.fixture_native_target_physical_acknowledgements WHERE run_id=v_run;
    IF FOUND THEN
        RETURN v_ack.receipt=p_receipt AND v_ack.receipt_sha256=p_receipt_sha256
          AND NOT EXISTS (SELECT 1 FROM public.storage_locations WHERE id=v_target.location_id)
          AND NOT EXISTS (SELECT 1 FROM public.storage_connections WHERE id=v_target.connection_id)
          AND EXISTS (SELECT 1 FROM public.storage_maintenance_gates WHERE location_id=v_target.location_id AND state='stopped');
    END IF;
    PERFORM 1 FROM public.storage_maintenance_gates
    WHERE location_id=v_target.location_id AND state='stopped' FOR UPDATE;
    IF NOT FOUND OR aap_assets_v1.fixture_native_target_references_retired(v_run) IS DISTINCT FROM true
       OR EXISTS (SELECT 1 FROM public.storage_operations WHERE location_id=v_target.location_id)
       OR EXISTS (SELECT 1 FROM public.storage_location_probes WHERE location_id=v_target.location_id)
       OR EXISTS (SELECT 1 FROM public.storage_locations WHERE connection_id=v_target.connection_id AND id<>v_target.location_id)
    THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_native_target_physical_acknowledgements(run_id,receipt,receipt_sha256)
    VALUES(v_run,p_receipt,p_receipt_sha256);
    DELETE FROM public.storage_locations WHERE id=v_target.location_id AND connection_id=v_target.connection_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_location_changed'; END IF;
    DELETE FROM public.storage_connections WHERE id=v_target.connection_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_connection_changed'; END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.acknowledge_fixture_native_target_physical_retirement(text,text) FROM PUBLIC;
