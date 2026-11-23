GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb)
    TO :"application_role";
REVOKE ALL ON TABLE yoao_assets_v1.reference_io_requests FROM :"application_role";
REVOKE EXECUTE ON FUNCTION yoao_assets_v1.capture_reference_io(uuid,text),
    yoao_assets_v1.recheck_reference_io(uuid,text) FROM :"application_role";
