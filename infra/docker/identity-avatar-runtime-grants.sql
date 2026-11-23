GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.reserve_avatar_upload(uuid,text,jsonb,text,integer),
    yoao_identity_v1.confirm_avatar_upload_version(uuid,text,jsonb,text,integer,text),
    yoao_identity_v1.avatar_upload_pending_matches(uuid,text,jsonb,text,integer,text),
    yoao_identity_v1.read_ready_avatar_upload(text),
    yoao_identity_v1.list_avatar_retirements(integer),
    yoao_identity_v1.begin_avatar_retirement(text),
    yoao_identity_v1.finish_avatar_retirement(text,jsonb,text,integer,text),
    yoao_identity_v1.read_avatar_binding_history(),
    yoao_identity_v1.avatar_saved_target_referenced(uuid)
    TO :"platform_runtime";
