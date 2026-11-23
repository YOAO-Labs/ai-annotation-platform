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
\ir reference-io-source-grants.sql
\ir saved-outputs-source-grants.sql
\ir media-io-source-grants.sql
\ir training-saved-io-source-grants.sql
\ir migration-io-source-grants.sql
\ir import-saved-io-source-grants.sql
COMMIT;
