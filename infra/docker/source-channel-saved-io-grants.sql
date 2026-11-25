\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.saved_io_login', :'assets_source_saved_io_login', true);
DO $guard$
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR session_user = current_setting('yoao.saved_io_login')
       OR NOT EXISTS (
           SELECT FROM pg_catalog.pg_roles r
           WHERE r.rolname = current_setting('yoao.saved_io_login')
             AND r.rolcanlogin AND NOT (
                 r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls OR r.rolreplication
             )
             AND NOT EXISTS (
                 SELECT FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid OR m.roleid = r.oid
             )
       ) THEN
        RAISE EXCEPTION 'Expected a dedicated, restricted Source Saved I/O LOGIN';
    END IF;
END;
$guard$;
GRANT CONNECT ON DATABASE :"expected_database" TO :"assets_source_saved_io_login";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.saved_io_connector(uuid,integer,jsonb),
    yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb),
    yoao_assets_v1.saved_io_allowlist(jsonb),
    yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean)
    TO :"assets_source_saved_io_login";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.capture_reference_io(uuid,text),
    yoao_assets_v1.recheck_reference_io(uuid,text) TO :"assets_source_saved_io_login";
GRANT USAGE ON SCHEMA yoao_assets_v1, yoao_export_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.saved_output_origin(jsonb,text,text),
    yoao_assets_v1.saved_avatar_subject(text,uuid,text),
    yoao_assets_v1.saved_bug_subject(uuid,uuid,text,uuid),
    yoao_assets_v1.saved_archive_subject(uuid,uuid,text),
    yoao_assets_v1.begin_saved_output_write(uuid,text,text,jsonb),
    yoao_assets_v1.finish_saved_output_write(uuid,text,text,jsonb),
    yoao_assets_v1.saved_output_write_status(uuid,text,text),
    yoao_export_v1.zip_io_subject(uuid,uuid,text),
    yoao_export_v1.media_member_io_subject(uuid,uuid,uuid,text),
    yoao_export_v1.media_prefix_io_subject(uuid,uuid,text),
    yoao_export_v1.save_source_zip_proof(uuid,uuid,jsonb),
    yoao_export_v1.save_source_media_version(uuid,uuid,uuid,text)
TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.media_io_artifact(text,text,uuid,uuid,boolean),
    yoao_assets_v1.media_io_cache_read(text,uuid,text,uuid),
    yoao_assets_v1.media_io_original_read(text,uuid,uuid),
    yoao_assets_v1.media_io_creation(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1.media_io_creation_evidence(uuid,uuid,text,jsonb),
    yoao_assets_v1.media_io_thumbnail(text,uuid,uuid,boolean),
    yoao_assets_v1.media_io_mask_upload(uuid,uuid,text),
    yoao_assets_v1.media_io_derivative(text,text,uuid,uuid,uuid,jsonb,boolean),
    yoao_assets_v1.media_io_original_cache(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1.media_io_prediction_input(uuid,uuid,text,text,boolean),
    yoao_assets_v1.media_io_retired_original(uuid,uuid,text,boolean)
TO :"assets_source_saved_io_login";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb),
    yoao_assets_v1.record_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb,jsonb)
TO :"assets_source_saved_io_login";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.migration_io_open_flight(uuid,uuid,text),
    yoao_assets_v1.migration_io_close_flight(uuid,uuid),
    yoao_assets_v1.migration_io_reconcile_flights(uuid),
    yoao_assets_v1.migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text),
    yoao_assets_v1.migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text),
    yoao_assets_v1.migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text),
    yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean),
    yoao_assets_v1.migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer)
TO :"assets_source_saved_io_login";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.import_saved_io_list(uuid,uuid,jsonb,jsonb),
    yoao_assets_v1.import_saved_io_action(jsonb,text,jsonb,jsonb)
TO :"assets_source_saved_io_login";
COMMIT;
