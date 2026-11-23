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
