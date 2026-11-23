GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb),
    yoao_assets_v1.record_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb,jsonb)
TO :"assets_source_saved_io_login";
