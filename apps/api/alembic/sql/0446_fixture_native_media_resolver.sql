-- The native control process uses a separate suite-scoped, read-only LOGIN to
-- route an already-authorized output key; ordinary business roles get no grant.
CREATE TABLE aap_assets_v1.fixture_native_resolver_principals (
    suite_id uuid PRIMARY KEY,
    principal_oid oid NOT NULL UNIQUE,
    principal_name name NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('active','retired'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_resolver_principals FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_resolver_principal()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.suite_id,NEW.principal_oid,NEW.principal_name) IS DISTINCT FROM
       (OLD.suite_id,OLD.principal_oid,OLD.principal_name)
       OR OLD.state<>'active' OR NEW.state<>'retired' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_resolver_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_resolver_principal() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_resolver_principal_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_resolver_principals FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_resolver_principal();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.register_fixture_native_resolver(p_suite uuid,p_role name)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_oid oid;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_resolver_owner_required';
    END IF;
    IF p_suite IS NULL OR p_role::text IS DISTINCT FROM
       'aap_fixture_media_lookup_' || pg_catalog.replace(p_suite::text,'-','') THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id=p_suite AND root.parent_run_id IS NULL AND root.family IN ('owned','screenshot') AND root.state='open'
    FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT role.oid INTO v_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname=p_role AND role.rolcanlogin AND NOT role.rolsuper
      AND NOT role.rolinherit AND NOT role.rolcreatedb AND NOT role.rolcreaterole
      AND NOT role.rolreplication AND NOT role.rolbypassrls;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_native_resolver_principals(suite_id,principal_oid,principal_name,state)
    VALUES (p_suite,v_oid,p_role,'active') ON CONFLICT DO NOTHING;
    RETURN EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_resolver_principals AS principal
        WHERE principal.suite_id=p_suite AND principal.principal_oid=v_oid
          AND principal.principal_name=p_role AND principal.state='active'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.register_fixture_native_resolver(uuid,name) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.resolve_fixture_native_cache(p_key text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_suite uuid; v_write record; v_origin text;
BEGIN
    SELECT principal.suite_id INTO v_suite FROM aap_assets_v1.fixture_native_resolver_principals AS principal
    JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
    WHERE principal.state='active' AND principal.principal_name=role.rolname AND role.rolname=SESSION_USER;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_resolver_required'; END IF;
    IF p_key IS NULL OR p_key='' OR pg_catalog.char_length(p_key)>2048 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_native_output_key_invalid';
    END IF;
    BEGIN
        SELECT candidate.* INTO STRICT v_write FROM (
            SELECT write.id,write.run_id,write.state
            FROM aap_assets_v1.fixture_native_cache_writes AS write
            WHERE write.object_key=p_key
            UNION ALL
            SELECT version.id,version.run_id,version.state
            FROM aap_assets_v1.fixture_media_versions AS version
            JOIN aap_assets_v1.fixture_independent_video_attempts AS attempt
              ON attempt.delivery_id=version.execution_id AND attempt.output_id=version.owner_id
             AND version.execution_kind='independent' AND version.owner_kind='video_output'
            JOIN aap_assets_v1.fixture_independent_media_births AS birth
              ON birth.delivery_id=attempt.delivery_id AND birth.run_id=version.run_id
            JOIN aap_assets_v1.fixture_independent_media_executions AS execution
              ON execution.delivery_id=attempt.delivery_id AND execution.token=version.execution_token
             AND execution.state='settled'
            JOIN public.video_derivative_outputs AS output
              ON output.id=attempt.output_id AND output.lease_token=attempt.lease_token
            JOIN public.dataset_items AS item
              ON item.id=output.dataset_item_id AND item.video_output_id=output.id
            WHERE version.object_key=p_key AND output.state='ready'
              AND output.result->'video'->>'poster_frame_path'=p_key
              AND output.inventory->'objects'->p_key=pg_catalog.jsonb_build_object(
                  'sha256',version.sha256,'size_bytes',version.size_bytes)
        ) AS candidate
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=candidate.run_id
          AND COALESCE(run.parent_run_id,run.id)=v_suite;
    EXCEPTION WHEN no_data_found THEN
        IF EXISTS (
            SELECT 1 FROM public.video_derivative_outputs AS output
            JOIN aap_assets_v1.fixture_media_births AS birth
              ON birth.owner_kind='video_output' AND birth.owner_id=output.id AND birth.generation=0
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=birth.run_id
              AND COALESCE(run.parent_run_id,run.id)=v_suite
            WHERE output.result->'video'->>'poster_frame_path'=p_key
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_unavailable';
        END IF;
        RETURN NULL;
    WHEN too_many_rows THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_ambiguous';
    END;
    SELECT reader.proxy_origin INTO v_origin FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    JOIN aap_assets_v1.fixture_native_read_principals AS reader
      ON reader.run_id=run.id AND reader.state='active'
    WHERE run.id=v_write.run_id AND root.id=v_suite AND run.state='open' AND root.state='open';
    IF NOT FOUND OR v_write.state<>'verified' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_unavailable';
    END IF;
    RETURN pg_catalog.jsonb_build_object('run_id',v_write.run_id,'write_id',v_write.id,'proxy_origin',v_origin);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.resolve_fixture_native_cache(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retire_fixture_native_resolver(p_suite uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_state text;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_resolver_owner_required';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id=p_suite AND root.parent_run_id IS NULL AND root.family IN ('owned','screenshot') FOR SHARE;
    IF NOT FOUND OR EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs
                            WHERE parent_run_id=p_suite AND state<>'retired')
       OR EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_runs AS root
           WHERE root.id=p_suite AND root.family='screenshot' AND (
               root.state NOT IN ('sealing','retired')
               OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_read_principals
                          WHERE run_id=p_suite AND state<>'retired')
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals
                          WHERE run_id=p_suite AND state<>'retired')
               OR (EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_suite)
                   AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_target_physical_acknowledgements
                                   WHERE run_id=p_suite))
               OR (NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_suite)
                   AND (EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births WHERE run_id=p_suite)
                     OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs WHERE run_id=p_suite)
                     OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_media_births WHERE run_id=p_suite)))
           )
       ) THEN RETURN false; END IF;
    SELECT state INTO v_state FROM aap_assets_v1.fixture_native_resolver_principals
    WHERE suite_id=p_suite FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF v_state='retired' THEN RETURN true; END IF;
    UPDATE aap_assets_v1.fixture_native_resolver_principals SET state='retired'
    WHERE suite_id=p_suite AND state='active';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retire_fixture_native_resolver(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_resolver_identity(p_suite uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_principal aap_assets_v1.fixture_native_resolver_principals%ROWTYPE;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_resolver_owner_required';
    END IF;
    SELECT * INTO v_principal FROM aap_assets_v1.fixture_native_resolver_principals WHERE suite_id=p_suite;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN pg_catalog.jsonb_build_object('oid',v_principal.principal_oid::bigint,
        'name',v_principal.principal_name,'state',v_principal.state);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_resolver_identity(uuid) FROM PUBLIC;
