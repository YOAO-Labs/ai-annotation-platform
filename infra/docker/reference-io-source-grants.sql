GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.capture_reference_io(uuid,text),
    yoao_assets_v1.recheck_reference_io(uuid,text) TO :"assets_source_saved_io_login";
