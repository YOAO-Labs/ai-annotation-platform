GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.import_saved_io_list(uuid,uuid,jsonb,jsonb),
    yoao_assets_v1.import_saved_io_action(jsonb,text,jsonb,jsonb)
TO :"assets_source_saved_io_login";
