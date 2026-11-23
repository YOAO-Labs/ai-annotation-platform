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
