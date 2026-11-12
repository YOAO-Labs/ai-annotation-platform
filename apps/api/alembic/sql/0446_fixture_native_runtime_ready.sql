-- Readiness proves the current process authenticated as the exact active
-- principal and loaded the immutable run target; it is not retirement proof.
CREATE FUNCTION aap_assets_v1.fixture_native_producer_ready(p_run uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'run_id',p_run,'endpoint',target.endpoint,'bucket',target.bucket,
        'storage_identity',target.storage_identity,'receipt_sha256',target.receipt_sha256,
        'database_oid',database.oid::bigint,'database_owner_marker',pg_catalog.shobj_description(database.oid,'pg_database'))
    FROM aap_assets_v1.fixture_native_cache_targets AS target
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=target.run_id
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    JOIN pg_catalog.pg_database AS database ON database.datname=pg_catalog.current_database()
    WHERE target.run_id=p_run AND run.state='open' AND root.state='open'
      AND aap_jobs_v1.fixture_media_principal_matches(p_run);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_producer_ready(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_reader_ready(p_run uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'run_id',p_run,'endpoint',target.endpoint,'bucket',target.bucket,
        'storage_identity',target.storage_identity,'receipt_sha256',target.receipt_sha256,
        'database_oid',database.oid::bigint,'database_owner_marker',pg_catalog.shobj_description(database.oid,'pg_database'))
    FROM aap_assets_v1.fixture_native_read_principals AS principal
    JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
    JOIN aap_assets_v1.fixture_native_cache_targets AS target ON target.run_id=principal.run_id
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=target.run_id
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    JOIN pg_catalog.pg_database AS database ON database.datname=pg_catalog.current_database()
    WHERE principal.run_id=p_run AND principal.state='active' AND role.rolname=SESSION_USER
      AND principal.principal_name=role.rolname AND run.state='open' AND root.state='open';
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_reader_ready(uuid) FROM PUBLIC;
