\set ON_ERROR_STOP on

GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer)
    TO :"platform_runtime";
REVOKE ALL ON TABLE yoao_assets_v1.saved_binding_probe FROM :"platform_runtime";
REVOKE ALL ON FUNCTION yoao_assets_v1.saved_io_connector(uuid,integer,jsonb),
    yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb),
    yoao_assets_v1.saved_io_allowlist(jsonb),
    yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean)
    FROM :"platform_runtime";
